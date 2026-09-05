from os import linesep
import logging
import threading
from typing import Any
import regex

from PySubtrans.Helpers.ContextHelpers import GetBatchContext
from PySubtrans.Helpers.Parse import FormatKeyValuePairs, ParseKeyValuePairs
from PySubtrans.Helpers.SubtitleHelpers import FindBestSplitIndex, MergeTranslations
from PySubtrans.Helpers.Localization import _
from PySubtrans.Helpers.Text import CompressWhitespace, Linearise, SanitiseSummary
from PySubtrans.Instructions import DEFAULT_TASK_TYPE, IsVietnameseLanguage, Instructions, default_polish_instructions, default_vietnamese_instructions
from PySubtrans.Substitutions import Substitutions
from PySubtrans.SubtitleLine import SubtitleLine
from PySubtrans.SubtitleProcessor import SubtitleProcessor
from PySubtrans.SubtitleValidator import SubtitleValidator
from PySubtrans.Translation import Translation
from PySubtrans.TranslationClient import TranslationClient
from PySubtrans.TranslationParser import TranslationParser
from PySubtrans.Options import Options, SettingsType
from PySubtrans.SubtitleBatch import SubtitleBatch

from PySubtrans.SubtitleError import NoProviderError, NoTranslationError, ProviderError, SubtitleError, TranslationAbortedError, TranslationError, TranslationImpossibleError
from PySubtrans.Helpers import FormatErrorMessages
from PySubtrans.Subtitles import Subtitles
from PySubtrans.SubtitleScene import SubtitleScene, UnbatchScenes
from PySubtrans.TranslationEvents import TerminologyUpdate, TranslationEvents
from PySubtrans.TranslationPrompt import TranslationPrompt
from PySubtrans.TranslationProvider import TranslationProvider
from PySubtrans.TranslationRequest import StreamingCallback

class SubtitleTranslator:
    """
    Processes subtitles into scenes and batches and sends them for translation
    """

    def __init__(self, settings : Options, translation_provider : TranslationProvider, resume : bool = False, terminology_map : dict[str,str]|None = None):
        """
        Initialise a SubtitleTranslator with translation options
        """
        self.events = TranslationEvents()
        self.lock = threading.Lock()
        self.aborted : bool = False
        self.errors : list[str|SubtitleError] = []
        self.lines_processed : int = 0

        self.max_lines = settings.get_int('max_lines')
        self.max_history = settings.get_int('max_context_summaries')
        self.stop_on_error = settings.get_bool('stop_on_error')
        self.retry_on_error = settings.get_bool('retry_on_error')
        self.polish_translation = settings.get_bool('polish_translation', False)
        self.polish_max_retries = settings.get_int('polish_max_retries') or 1
        self.split_on_error = settings.get_bool('autosplit_on_error')
        self.build_terminology_map = settings.get_bool('build_terminology_map')
        configured_terminology = ParseKeyValuePairs(settings.get('terminology_map', {}))
        self.terminology_map : dict[str, str] = dict(configured_terminology)
        if terminology_map:
            # Explicitly supplied maps are authoritative over project settings.
            self.terminology_map.update(terminology_map)
        self.user_terminology_map : dict[str, str] = dict(self.terminology_map)
        self.max_summary_length = settings.get_int('max_summary_length')
        self.retranslate = settings.get_bool('retranslate')
        self.reparse = settings.get_bool('reparse')
        self.preview = settings.get_bool('preview')
        self.resume = resume and (not self.reparse and not self.retranslate)

        settings = Options(settings)

        self.instructions : Instructions = settings.GetInstructions()
        self.task_type : str = self.instructions.task_type or DEFAULT_TASK_TYPE
        self.user_prompt : str = settings.BuildUserPrompt()

        self.system_instructions : str = self.instructions.instructions or ''
        target_language = settings.get_str('target_language', '') or settings.get_str('to_language', '') or ''
        if IsVietnameseLanguage(target_language):
            self.system_instructions = f"{self.system_instructions}\n\n{default_vietnamese_instructions}".strip()
        if self.build_terminology_map and self.instructions.terminology_instructions:
            self.system_instructions = f"{self.system_instructions}\n\n{self.instructions.terminology_instructions}".strip()

        substitutions_mode = settings.get_str('substitution_mode') or Substitutions.Mode.Auto
        substitutions_list = settings.get('substitutions', {})
        if not isinstance(substitutions_list, (dict, list, str)):
            logging.warning(_("Invalid substitutions list, must be a dictionary, list or string"))
            substitutions_list = {}

        self.substitutions = Substitutions(substitutions_list, substitutions_mode)

        self.settings : SettingsType = settings.GetSettings()
        self.settings['instructions'] = self.system_instructions
        self.settings['retry_instructions'] = self.instructions.retry_instructions

        logging.debug(f"Translation prompt: {self.user_prompt}")

        self.translation_provider : TranslationProvider = translation_provider

        if not self.translation_provider:
            raise NoProviderError()

        try:
            self.client : TranslationClient = self.translation_provider.GetTranslationClient(self.settings)

        except Exception as e:
            raise ProviderError(_("Unable to create provider client: {error}").format(error=str(e)), translation_provider)

        if not self.client:
            raise ProviderError(_("Unable to create translation client"), translation_provider)

        self.client.SetEvents(self.events)

        self.postprocessor = SubtitleProcessor(settings) if settings.get('postprocess_translation') else None
        self.validator = SubtitleValidator(settings)

    def StopTranslating(self):
        self.aborted = True
        self.client.AbortTranslation()

    def TranslateSubtitles(self, subtitles : Subtitles):
        """
        Translate a SubtitleFile
        """
        if not subtitles:
            raise TranslationImpossibleError(_("No subtitles to translate"))

        if subtitles.scenes and self.resume:
            self._emit_info(_("Resuming translation"))

        if not subtitles.scenes:
            raise TranslationImpossibleError(_("Subtitles must be batched before translation"))

        self._emit_info(_("Translating {linecount} lines in {scenecount} scenes").format(linecount=subtitles.linecount, scenecount=subtitles.scenecount))

        self.events.preprocessed.send(self, scenes=subtitles.scenes)

        # Iterate over each subtitle scene and request translation
        for scene in subtitles.scenes:
            if self.aborted:
                break

            if self.max_lines and self.lines_processed >= self.max_lines:
                break

            if self.resume and scene.all_translated:
                self._emit_info(_("Scene {scene} already translated {linecount} lines...").format(scene=scene.number, linecount=scene.linecount))
                continue

            logging.debug(f"Translating scene {scene.number} of {subtitles.scenecount}")
            batch_numbers =[ batch.number for batch in scene.batches if not batch.translated ] if self.resume else None

            self.TranslateScene(subtitles, scene, batch_numbers=batch_numbers)

            if self.errors and self.stop_on_error:
                self._emit_error(_("Failed to translate scene {scene}... stopping translation").format(scene=scene.number))
                return

        if self.aborted:
            self._emit_info(_("Translation aborted"))
            return

        # Linearise the translated scenes
        originals, translations, untranslated = UnbatchScenes(subtitles.scenes)

        if translations:
            self._emit_info(_("Successfully translated {count} lines!").format(count=len(translations)))

        if untranslated and not self.max_lines and not self.preview:
            logging.warning(_("Failed to translate {count} lines:").format(count=len(untranslated)))
            for line in untranslated:
                self._emit_info(_("Untranslated > {number}. {text}").format(number=line.number, text=line.text))

        subtitles.originals = originals
        subtitles.translated = translations

    def TranslateScene(self, subtitles : Subtitles, scene : SubtitleScene, batch_numbers = None, line_numbers = None):
        """
        Send a scene for translation
        """
        try:
            batches = [ batch for batch in scene.batches if batch.number in batch_numbers ] if batch_numbers else scene.batches
            context = {}

            for batch in batches:
                context = GetBatchContext(subtitles, scene.number, batch.number, self.max_history)

                with self.lock:
                    terminology_snapshot = dict(self.terminology_map) if self.terminology_map else None

                if terminology_snapshot:
                    formatted = FormatKeyValuePairs(terminology_snapshot)
                    context['terminology'] = formatted
                    batch.AddContext('terminology', formatted)

                try:
                    self.TranslateBatch(batch, line_numbers, context)

                except TranslationImpossibleError as e:
                    # If batch has any translated lines from streaming, validate them before re-raising
                    if batch.any_translated:
                        self.validator.ValidateBatch(batch)
                    raise

                except TranslationError as e:
                    self._emit_warning(_("Error translating scene {scene} batch {batch}: {error}").format(scene=batch.scene, batch=batch.number, error=str(e)))
                    batch.errors.append(e)

                if self.aborted:
                    return

                # Notify observers the batch was translated
                self.events.batch_translated.send(self, batch=batch)

                if self.build_terminology_map:
                    self._update_terminology_map(batch)

                if batch.errors:
                    self._emit_warning(_("Errors encountered translating scene {scene} batch {batch}").format(scene=batch.scene, batch=batch.number))
                    scene.errors.extend(batch.errors)
                    self.errors.extend(batch.errors)

                if batch.errors and self.stop_on_error:
                    return

                if self.max_lines and self.lines_processed >= self.max_lines:
                    self._emit_info(_("Reached max_lines limit of ({lines} lines)... finishing").format(lines=self.max_lines))
                    break

            # Update the scene summary based on the best available information (we hope)
            scene.summary = self._get_best_summary([scene.summary, context.get('scene'), context.get('summary')])

            if self.polish_translation and not self.preview and not self.aborted:
                self.PolishScene(subtitles, scene)

            # Notify observers the scene was translated
            self.events.scene_translated.send(self, scene=scene)

        except (TranslationAbortedError, TranslationImpossibleError) as e:
            raise

    def PolishScene(self, subtitles : Subtitles, scene : SubtitleScene) -> None:
        """Polish a translated scene and keep the original if validation fails."""
        originals = [line for batch in scene.batches for line in batch.originals]
        translated = [line for batch in scene.batches for line in batch.translated if line.text]
        if not originals or len(originals) != len(translated):
            self._emit_warning(_("Skipping polish for scene {scene}: incomplete translation").format(scene=scene.number))
            return

        context = GetBatchContext(subtitles, scene.number, scene.batches[0].number, self.max_history)
        context['current_translation'] = '\n'.join(
            f"#{line.number}\nTranslation>\n{line.text or ''}" for line in translated
        )
        context['polish_scope'] = f"Scene {scene.number}"

        prompt = self.client.BuildTranslationPrompt(
            self.user_prompt,
            self.instructions.polish_instructions or default_polish_instructions,
            originals,
            context,
        )
        polished = self.client.RequestTranslation(prompt)
        if not polished:
            self._emit_warning(_("Polish returned no translation for scene {scene}").format(scene=scene.number))
            return

        try:
            prompt.RestoreMarkup(polished)
            parser = self.client.GetParser(self.task_type)
            parser.ProcessTranslation(polished)
            candidate, unmatched = parser.MatchTranslations(originals)
        except TranslationError as error:
            self._emit_warning(_("Rejected polish for scene {scene}: {error}").format(scene=scene.number, error=error))
            return

        if not self._validate_polish(originals, candidate, unmatched, parser.errors):
            self._emit_warning(_("Rejected polish for scene {scene}; keeping original translation").format(scene=scene.number))
            return

        by_number = {line.number: line for line in candidate}
        for batch in scene.batches:
            for line in batch.translated:
                replacement = by_number.get(line.number)
                if replacement:
                    line.text = replacement.text
        self._emit_info(_("Polished scene {scene}").format(scene=scene.number))

    def _validate_polish(self, originals : list[SubtitleLine], candidate : list[SubtitleLine], unmatched : list[SubtitleLine], errors : list[Exception]) -> bool:
        """Accept polish only when alignment, markup and glossary remain valid."""
        if unmatched or errors or len(candidate) != len(originals):
            return False
        original_numbers = [line.number for line in originals]
        candidate_numbers = [line.number for line in candidate]
        if original_numbers != candidate_numbers:
            return False
        for source, output in zip(originals, candidate):
            if not output.text or '__SUBTITLE_MARKUP_' in output.text:
                return False
            for term, target in self.user_terminology_map.items():
                if self._contains_term(source.text or '', term) and not self._contains_term(output.text, target):
                    return False
        return True

    def TranslateBatch(self, batch : SubtitleBatch, line_numbers : list[int]|None, context : dict[str,Any]|None):
        """
        Send batches of subtitles for translation, building up context.
        """
        if self.aborted:
            return

        if self.reparse and batch.translation:
            self._emit_info(_("Reparsing scene {scene} batch {batch} with {count} lines...").format(scene=batch.scene, batch=batch.number, count=len(batch.originals)))
            self.ProcessBatchTranslation(batch, batch.translation, line_numbers)
            return

        if self.resume and not self.retranslate and batch.all_translated:
            self._emit_info(_("Scene {scene} batch {batch} already translated {lines} lines...").format(scene=batch.scene, batch=batch.number, lines=batch.size))
            return

        originals, context = self.PreprocessBatch(batch, context)

        logging.debug(f"Translating scene {batch.scene} batch {batch.number} with {len(originals)} lines...")

        # Build summaries context
        context['batch'] = f"Scene {batch.scene} batch {batch.number}"
        if batch.summary:
            context['summary'] = batch.summary

        instructions = self.system_instructions
        if not instructions:
            raise TranslationImpossibleError(_("No instructions provided for translation"))

        batch.prompt = self.client.BuildTranslationPrompt(self.user_prompt, instructions, originals, context)

        if self.preview:
            return

        # Ask the client to do the translation
        streaming_callback = self._create_streaming_callback(batch, line_numbers) if self.client.enable_streaming else None
        translation : Translation|None = self.client.RequestTranslation(batch.prompt, streaming_callback=streaming_callback)

        if not self.aborted:
            if not translation:
                raise TranslationError(_("Unable to translate scene {scene} batch {batch}").format(scene=batch.scene, batch=batch.number))

            # Process the response first — translation may be complete even if the token limit was hit
            batch.prompt.RestoreMarkup(translation)
            self.ProcessBatchTranslation(batch, translation, line_numbers)

            # Consider splitting the batch in half if there were errors (preferred strategy)
            split_performed = False
            if batch.errors and self.split_on_error and len(batch.originals) >= 2:
                split_performed = self._translate_split_batch(batch, line_numbers, context or {}, original_translation=translation)

            # If no split was performed, retry without context when the token limit was reached with errors
            if not split_performed and batch.errors and translation.reached_token_limit:
                logging.warning(_("Hit API token limit with errors, retrying batch without context..."))
                batch.prompt.GenerateMessages(instructions, batch.originals, {})
                translation = self.client.RequestTranslation(batch.prompt, streaming_callback=streaming_callback)
                if translation and not self.aborted:
                    self.ProcessBatchTranslation(batch, translation, line_numbers)

            # Consider retrying if there were errors and no other recovery strategy was applied
            if not split_performed and batch.errors and self.retry_on_error:
                logging.warning(_("Scene {scene} batch {batch} failed validation, requesting retranslation").format(scene=batch.scene, batch=batch.number))
                self.RequestRetranslation(batch, line_numbers=line_numbers, context=context)

            # Update the context, unless it's a retranslation pass
            if translation and not self.retranslate and not self.aborted:
                context['summary'] = self._get_best_summary([translation.summary, batch.summary])
                context['scene'] = self._get_best_summary([translation.scene, context.get('scene')])
                context['synopsis'] = translation.synopsis or context.get('synopsis', "")
                #context['names'] = translation.names or context.get('names', []) or options.get('names')
                batch.UpdateContext(context)

    def PreprocessBatch(self, batch : SubtitleBatch, context : dict[str,Any]|None = None) -> tuple[list[SubtitleLine], dict[str, Any]]:
        """
        Preprocess the batch before translation
        """
        context = context or {}
        if batch.context and (self.retranslate or self.reparse):
            # If it's a retranslation, restore context from the batch
            for key, value in batch.context.items():
                context[key] = value

        # Apply any substitutions to the input
        replacements = batch.PerformInputSubstitutions(self.substitutions)

        if replacements:
            replaced : list[str] = [f"{Linearise(k)} -> {Linearise(v)}" for k,v in replacements.items()]
            self._emit_info(_("Made substitutions in input:\n{replaced}").format(replaced=linesep.join(replaced)))
            batch.AddContext('replacements', replaced)

        # Filter out empty lines
        originals = [ line for line in batch.originals if line.text and line.text.strip() ]

        # Apply the max_lines limit
        with self.lock:
            line_count = min(self.max_lines - self.lines_processed, len(originals)) if self.max_lines else len(originals)
            self.lines_processed += line_count
            if len(originals) > line_count:
                self._emit_info(_("Truncating batch to remain within max_lines"))
                originals = originals[:line_count] if line_count > 0 else []

        return originals, context

    def ProcessBatchTranslation(self, batch : SubtitleBatch, translation : Translation, line_numbers : list[int]|None):
        """
        Attempt to extract translation from the API response
        """
        if not translation:
            raise NoTranslationError(_("No translation provided"))

        if not translation.has_translation:
            raise TranslationError(_("Translation contains no translated text"), translation=translation)

        logging.debug(f"Scene {batch.scene} batch {batch.number} translation:\n{translation.text}\n")

        # Apply the translation to the subtitles
        parser : TranslationParser = self.client.GetParser(self.task_type)

        parser.ProcessTranslation(translation)

        # Try to match the translations with the original lines
        translated, unmatched = parser.MatchTranslations(batch.originals)

        # Assign the translated lines to the batch
        if line_numbers:
            translated = [line for line in translated if line.number in line_numbers]

        self._apply_terminology_overrides(batch.originals, translated)
        batch._translated = MergeTranslations(batch.translated or [], translated)

        batch.translation = translation
        batch.errors = [err for err in parser.errors if isinstance(err, str) or isinstance(err, SubtitleError)]

        # Emit any warnings from the parser
        for warning in parser.warnings:
            self._emit_warning(warning)

        if batch.untranslated and not self.max_lines:
            self._emit_warning(_("Unable to match {count} lines with a source line").format(count=len(unmatched)))
            batch.AddContext('untranslated_lines', [f"{item.number}. {item.text}" for item in batch.untranslated])

        # Apply any word/phrase substitutions to the translation
        replacements = batch.PerformOutputSubstitutions(self.substitutions)

        if replacements:
            replaced = [f"{k} -> {v}" for k,v in replacements.items()]
            self._emit_info(_("Made substitutions in output:\n{replaced}").format(replaced=linesep.join(replaced)))

        # Perform substitutions on the output
        translation.PerformSubstitutions(self.substitutions)

        # Post-process the translation
        if self.postprocessor:
            batch._translated = self.postprocessor.PostprocessSubtitles(batch.translated)

            self._emit_info(_("Scene {scene} batch {batch}: {translated} lines and {untranslated} untranslated.").format(
                scene=batch.scene, 
                batch=batch.number, 
                translated=len(batch.translated or []), 
                untranslated=len(batch.untranslated or []))
                )

        if translation.summary and translation.summary.strip():
            self._emit_info(_("Summary: {summary}").format(summary=translation.summary))

    def RequestRetranslation(self, batch : SubtitleBatch, line_numbers : list[int]|None = None, context : dict[str, str]|None = None):
        """
        Ask the client to retranslate the input and correct errors
        """
        translation : Translation|None = batch.translation
        if not translation:
            raise TranslationError(_("No translation to retranslate"))

        prompt : TranslationPrompt|None = batch.prompt
        if not prompt or not prompt.messages:
            raise TranslationError(_("No prompt to retranslate"))

        if not self.instructions.retry_instructions:
            raise TranslationError(_("No retry instructions provided"))

        if not translation.text:
            raise TranslationError(_("No translation text to retranslate"), translation=translation)

        retry_instructions = self.instructions.retry_instructions
        if retry_instructions is None:
            return

        prompt.GenerateRetryPrompt(translation.text, retry_instructions, batch.errors)

        # Let's raise the temperature a little bit
        temperature = self.client.temperature or 0.0
        retry_temperature = min(temperature + 0.1, 1.0)

        retranslation : Translation|None = self.client.RequestTranslation(prompt, retry_temperature)

        if self.aborted:
            return None

        if not isinstance(retranslation, Translation):
            raise TranslationError(_("Retranslation is not the expected type"), translation=retranslation)

        logging.debug(f"Scene {batch.scene} batch {batch.number} retranslation:\n{retranslation.text}\n")

        prompt.RestoreMarkup(retranslation)
        self.ProcessBatchTranslation(batch, retranslation, line_numbers)

        if batch.errors:
            self._emit_warning(_("Retry failed validation: {errors}").format(errors=FormatErrorMessages(batch.errors)))
        else:
            self._emit_info(_("Retry passed validation"))

    def _translate_split_batch(self, batch : SubtitleBatch, line_numbers : list[int]|None, context : dict[str,Any], original_translation : Translation|None = None) -> bool:
        """
        Split the batch originals in half and translate each half separately, merging results.
        Used as a fallback when a full-batch translation has errors.
        If original_translation is provided, its context fields are enriched with any values
        gleaned from the half responses (priority: original → first half → second half).
        Returns True if a split was attempted, False if no split could be performed.
        """
        originals = batch.originals

        split_index = FindBestSplitIndex(originals)
        if split_index is None:
            return False

        instructions = self.system_instructions
        if not instructions:
            return False

        self._emit_info(_("Splitting scene {scene} batch {batch} into two halves for retranslation...").format(
            scene=batch.scene, batch=batch.number))

        # Phase 1: collect raw translations from each half without processing
        half_translations : list[Translation] = []
        half_output_texts : list[str] = []
        api_errors : list[str|SubtitleError] = []

        for half_originals in [originals[:split_index], originals[split_index:]]:
            if self.aborted:
                return False

            prompt = self.client.BuildTranslationPrompt(self.user_prompt, instructions, half_originals, context)
            half_translation : Translation|None = self.client.RequestTranslation(prompt)

            if not half_translation:
                api_errors.append(TranslationError(_("No translation returned for batch half")))
            else:
                prompt.RestoreMarkup(half_translation)
                # Some providers may echo the complete source batch even when
                # asked for a half. Restrict split output to this half before
                # merging, otherwise valid line numbers appear duplicated.
                half_numbers = {line.number for line in half_originals}
                half_parser = self.client.GetParser(self.task_type)
                half_parser.ProcessTranslation(half_translation, validate=False)
                filtered_lines = [line for line in half_parser.translated if line.number in half_numbers]
                if filtered_lines:
                    half_output_texts.append('\n\n'.join(
                        f"#{line.number}\nTranslation>\n{line.text or ''}" for line in filtered_lines
                    ))
                else:
                    half_output_texts.append(half_translation.text or '')
                half_translations.append(half_translation)

        # Phase 2: merge translation texts and delegate all output handling to ProcessBatchTranslation
        if not half_translations:
            batch.errors = api_errors
            return False

        merged_text = "\n".join(text for text in half_output_texts if text)
        merged_translation = Translation({'text': merged_text})
        merged_terminology : dict[str, str] = {}
        for half_translation in half_translations:
            if half_translation.terminology:
                merged_terminology.update(half_translation.terminology)
        if merged_terminology:
            merged_translation.content['terminology'] = merged_terminology

        try:
            self.ProcessBatchTranslation(batch, merged_translation, line_numbers)
        except TranslationError as e:
            batch.errors = (batch.errors or []) + [e] + api_errors
            return False

        if api_errors:
            batch.errors = (batch.errors or []) + api_errors

        # Phase 3: enrich the original translation's context with values from the halves,
        # preserving any context the original already had (original → half1 → half2)
        if original_translation:
            all_sources = [original_translation] + half_translations
            original_translation.content['summary'] = next((t.summary for t in all_sources if t.summary), None)
            original_translation.content['scene']   = next((t.scene   for t in all_sources if t.scene),   None)
            original_translation.content['synopsis']= next((t.synopsis for t in all_sources if t.synopsis), None)
            merged_terminology = {}
            for source in all_sources:
                if source.terminology:
                    merged_terminology.update(source.terminology)
            if merged_terminology:
                original_translation.content['terminology'] = merged_terminology

        # Split responses contain terminology learned from the individual
        # halves. Persist it immediately; the normal scene loop may otherwise
        # only inspect the original full-batch response. Use the source-content
        # check here, while the normal path also requires target occurrence in
        # output (the split response has already been filtered/reconstructed).
        split_source_text = CompressWhitespace(' '.join(line.text or '' for line in batch.originals))
        with self.lock:
            for term, proposed in merged_terminology.items():
                term_norm = str(term).strip()
                proposed_norm = str(proposed).strip()
                if (self._is_reliable_terminology(term_norm, proposed_norm)
                        and self._contains_term(split_source_text, term_norm)
                        and term_norm not in self.user_terminology_map
                        and term_norm not in self.terminology_map):
                    self.terminology_map[term_norm] = proposed_norm

        if batch.errors:
            self._emit_warning(_("Split retranslation has errors: {errors}").format(errors=FormatErrorMessages(batch.errors)))
        else:
            self._emit_info(_("Split retranslation passed validation"))

        return True

    def _get_best_summary(self, candidates : list[str|None]) -> str|None:
        """
        Generate a summary of the translated subtitles
        """
        movie_name : str|None = self.settings.get_str( 'movie_name', None)
        movie_name = str(movie_name).strip() if movie_name else None
        max_length : int|None = self.max_summary_length

        for candidate in candidates:
            if candidate is None:
                continue

            sanitised = SanitiseSummary(candidate, movie_name, max_length)
            if sanitised:
                if len(sanitised) < len(candidate):
                    self._emit_info(_("Summary was truncated from {original} to {truncated} characters").format(original=len(candidate), truncated=len(sanitised)))
                return sanitised

        return None

    def _create_streaming_callback(self, batch : SubtitleBatch, line_numbers : list[int]|None) -> StreamingCallback:
        """
        Create a streaming callback that processes partial translations and emits batch_updated events
        """
        def streaming_callback(partial_translation : Translation):
            if self.aborted or not partial_translation:
                return

            try:
                # Process the partial translation (without validation)
                self._process_partial_translation(batch, partial_translation, line_numbers)

                # Emit batch_updated event with the updated batch
                self.events.batch_updated.send(self, batch=batch)

            except Exception as e:
                logging.warning(_("Error processing streaming update for scene {scene} batch {batch}: {error}").format(scene=batch.scene, batch=batch.number, error=e))

        return streaming_callback

    def _process_partial_translation(self, batch : SubtitleBatch, translation : Translation, line_numbers : list[int]|None):
        """
        Process a partial translation without validation (streaming updates only)
        """
        if not translation or not translation.has_translation:
            return

        parser = self.client.GetParser(self.task_type)
        try:
            parser.ProcessTranslation(translation, validate=False)

            translated, _ = parser.MatchTranslations(batch.originals)

            if line_numbers:
                translated = [line for line in translated if line.number in line_numbers]

            # Merge with existing translations (MergeTranslations is already imported at top)
            # Todo: we should use a SubtitleEditor to merge changes 
            batch.translated = MergeTranslations(batch.translated or [], translated)

            # Note: We don't set errors for partial translations to avoid false validation failures

        except Exception:
            pass

    def _contains_term(self, text: str, term: str) -> bool:
        """Match a terminology entry without corrupting Vietnamese words."""
        if not text or not term:
            return False
        escaped = regex.escape(term.strip())
        # Vietnamese uses Latin letters with combining marks, so \w provides
        # the correct boundary for most words. CJK terms have no spaces and
        # intentionally use substring matching.
        if regex.search(r'\p{Letter}', term) and not regex.search(r'\p{Script=Han}|\p{Script=Hiragana}|\p{Script=Katakana}', term):
            return bool(regex.search(rf'(?<![\p{{L}}\p{{M}}\p{{N}}_]){escaped}(?![\p{{L}}\p{{M}}\p{{N}}_])', text, regex.IGNORECASE))
        return term.casefold() in text.casefold()

    def _is_reliable_terminology(self, source : str, target : str) -> bool:
        """Reject trivial or overly generic model terminology suggestions."""
        if len(source) < 2 or len(target) < 1:
            return False
        if source.casefold() == target.casefold():
            return False
        if source.isdigit() or target.isdigit():
            return False
        # Do not learn isolated function words, punctuation, or a single
        # alphabetic token that is likely ordinary dialogue.
        if len(source.split()) == 1 and source.isascii() and source.isalpha() and len(source) < 4:
            return False
        return True

    def _apply_terminology_overrides(self, originals : list[SubtitleLine], translated : list[SubtitleLine]) -> None:
        """Apply safe, user-defined terminology overrides to translated lines."""
        if not self.user_terminology_map:
            return

        original_by_number = {line.number: line.text or '' for line in originals}
        terms = sorted(self.user_terminology_map.items(), key=lambda item: len(item[0]), reverse=True)
        for line in translated:
            source = original_by_number.get(line.number, '')
            if not source or not line.text:
                continue
            updated = line.text
            for original, target in terms:
                if not original or not target or original not in source:
                    continue
                # Do not re-expand a correct target containing the source term.
                if original != target and original in target:
                    continue
                pattern = regex.compile(rf'(?<!\\w){regex.escape(original)}(?!\\w)', regex.IGNORECASE)
                updated = pattern.sub(lambda _: target, updated)
            line.text = updated

    def _update_terminology_map(self, batch : SubtitleBatch):
        """
        Merge terminology returned by a batch translation into self.terminology_map.
        Only new terms are added; existing entries are preserved to avoid data loss.
        """
        if not batch.translation or not batch.translation.terminology:
            return

        returned_terms = batch.translation.terminology
        new_terms : dict[str, str] = {}
        conflict_terms : dict[str, tuple[str, str]] = {}

        original_text = CompressWhitespace(' '.join(line.text or '' for line in batch.originals))
        translated_text = CompressWhitespace(' '.join(line.text or '' for line in batch.translated))

        with self.lock:
            for term, proposed in returned_terms.items():
                term_norm = str(term).strip()
                proposed_norm = str(proposed).strip()

                if not self._is_reliable_terminology(term_norm, proposed_norm):
                    continue

                if term_norm == proposed_norm:
                    continue

                # Orient the pair using batch content as ground truth.
                # Swap if the key appears in translated but not originals.
                if self._contains_term(translated_text, term_norm) and not self._contains_term(original_text, term_norm):
                    term, proposed = proposed, term
                    term_norm, proposed_norm = proposed_norm, term_norm

                # Reject if the source term doesn't appear in originals — it's hallucinated.
                if not self._contains_term(original_text, term_norm):
                    continue

                # Reject if the proposed translation doesn't appear in translated —
                # canonising unused renderings would push future batches toward them.
                if not self._contains_term(translated_text, proposed_norm):
                    continue

                # User-provided terminology is authoritative and must never be
                # replaced by an inferred model suggestion.
                if term_norm in self.user_terminology_map:
                    continue

                existing = self.terminology_map.get(term)
                if existing is None:
                    new_terms[term] = proposed
                elif existing != proposed:
                    conflict_terms[term] = (existing, proposed)

            self.terminology_map.update(new_terms)
            snapshot : dict[str, str] = dict(self.terminology_map)

        update = TerminologyUpdate(
            terminology_map=snapshot,
            scene=batch.scene,
            batch=batch.number,
            returned_terms=returned_terms,
            new_terms=new_terms,
            conflict_terms=conflict_terms,
        )
        self.events.terminology_updated.send(self, update=update)

    def _emit_error(self, message : str):
        """Emit an error event"""
        self.events.error.send(self, message=message)

    def _emit_warning(self, message : str):
        """Emit a warning event"""
        self.events.warning.send(self, message=message)

    def _emit_info(self, message : str):
        """Emit an info event"""
        self.events.info.send(self, message=message)
