from datetime import timedelta
import logging
from typing import Any
import regex

from PySubtrans.Instructions import DEFAULT_TASK_TYPE
from PySubtrans.Options import Options
from PySubtrans.Helpers.Localization import _
from PySubtrans.Helpers.SubtitleHelpers import MergeTranslations
from PySubtrans.Helpers.Text import IsTextContentEqual
from PySubtrans.SubtitleLine import SubtitleLine
from PySubtrans.SubtitleError import NoTranslationError, TranslationError, UntranslatedLinesError
from PySubtrans.SubtitleValidator import SubtitleValidator
from PySubtrans.Translation import Translation

default_pattern = (
    r"#(?P<number>\d+)"
    r"(?:[\s\r\n]+Original>[\s\r\n]+(?P<original>[\s\S]*?))?"
    r"[\s\r\n]+Translation>"
    r"(?:[\s\r\n]?(?P<body>[\s\S]*?))?"
    r"(?=\n#\d|\Z)"
)

fallback_patterns = [
    r"#(?P<number>\d+)(?:[\s\r\n]+Original>[\s\r\n]+(?P<original>[\s\S]*?))?[\s\r\n]*(?:Translation>(?:[\s\r\n]+(?P<body>[\s\S]*?))?(?:(?=\n{2,})|\Z))",
    r"#(?P<number>\d+)(?:[\s\r\n]+Original[>:][\s\r\n]+(?P<original>[\s\S]*?))?[\s\r\n]*(?:Translation[>:](?:[\s\r\n]+(?P<body>[\s\S]*?))?(?:(?=\n{2,})|\Z))",
    r"#(?P<number>\d+)(?:[\s\r\n]+Original[>:][\s\r\n]+(?P<original>[\s\S]*?))?[\s\r\n]*Translation[>:][\s\r\n]+(?P<body>[\s\S]*?)(?=(?:\n{2,}#)|\Z)",
    r"#(?P<number>\d+)(?:[\s\r\n]*Original[>:][\s\r\n]*(?P<original>[\s\S]*?))?[\s\r\n]*Translation[>:][\s\r\n]*(?P<body>[\s\S]*?)(?=(?:\n{2,}#)|\Z)",
    r"#(?P<number>\d+)[\s\r\n]+Translation[>:][\s\r\n]+(?P<body>[\s\S]*?)(?=(?:\n{2,}#)|\Z)",
    r"#(?P<number>\d+)(?:[\s\r\n]+(?P<body>[\s\S]*?))?(?:(?=\n{2,})|\Z)"  # Just the number and translation
    ]

class TranslationParser:
    """
    Extract translated subtitles from the AI translation response
    """
    def __init__(self, task_type : str, options : Options):
        self.options : Options = options
        self.text : str|None = None
        self.translations : dict[int|str, SubtitleLine] = {}
        self.translated : list[SubtitleLine] = []
        self.errors : list[Exception] = []
        self.warnings : list[str] = []
        self.metatags : list[str] = ["summary", "scene", "terminology"]
        self.task_type : str = task_type
        self.regex_patterns : list[regex.Pattern[Any]] = self.GetRegularExpressionPatterns(task_type)

    def GetRegularExpressionPatterns(self, task_type : str = DEFAULT_TASK_TYPE) -> list[regex.Pattern[Any]]:
        """
        Returns a list of regular expressions to try for extracting translations
        """
        # Use the current default pattern, and fall back on alternative/older patterns if no matches are found
        patterns = [
            regex.compile(
                pattern.replace(DEFAULT_TASK_TYPE, task_type), regex.MULTILINE) for pattern in [default_pattern] + fallback_patterns
            ]
        return patterns

    def ProcessTranslation(self, translation : Translation, validate : bool = True) -> list[SubtitleLine]|None:
        """
        Extract lines from a batched translation, using the
        pre-defined pattern to match each line, or a list of fallbacks
        if the match fails.
        """
        self.text = translation.text if isinstance(translation, Translation) else str(translation)

        if not self.text:
            raise TranslationError("No translated text provided", translation=translation)

        matches : list[dict[str,str]] = []
        duplicate_response_numbers: set[str] = set()
        for template in self.regex_patterns:
            matches = self.FindMatches(f"{self.text}\n\n", template)

            if matches:
                break

        if not matches:
            logging.warning(f"No matches found in response (first 200 chars): {self.text[:200]!r}")
            raise TranslationError(f"No matches found in translation text using patterns: {self.regex_patterns}", translation=translation)

        logging.debug(f"Matches: {str(matches)}")

        subs = [SubtitleLine(match) for match in matches]
        duplicate_keys = {sub.key for sub in subs if sum(item.key == sub.key for item in subs) > 1}
        if duplicate_keys:
            self.errors.append(TranslationError(
                f"Duplicate translation line numbers found: {sorted(duplicate_keys)}",
                translation=self.text))

        self.translations = {
            sub.key: sub for sub in subs
            }

        if not self.translations:
            return None

        self.translated = MergeTranslations(self.translated, list(self.translations.values()))

        if validate:
            validation_errors = self.ValidateTranslations()
            self.errors = validation_errors
            if self.errors and self.translated:
                self._fix_unclosed_tags()
                self.errors = self.ValidateTranslations()

        if duplicate_keys:
            self.errors.append(TranslationError(
                f"Duplicate translation line numbers found: {sorted(duplicate_keys)}",
                translation=self.text))
        return self.translated

    def FindMatches(self, text, template) -> list[dict[str,str]]:
        """
        re.findall has some very unhelpful behaviour, so we use finditer instead.
        """
        return [{
            'body': match.group('body'),
            'number': match.groupdict().get('number'),
            'start': match.groupdict().get('start'),
            'end': match.groupdict().get('end'),
            'original': match.groupdict().get('original')
            } for match in template.finditer(text)]

    def MatchTranslations(self, originals : list[SubtitleLine]) -> tuple[list[SubtitleLine], list[SubtitleLine]]:
        """
        Match lines in the translation with the original subtitles
        """
        if not originals:
            raise ValueError("Original subtitles not provided")

        matched = []
        unmatched = []
        original_keys = {item.key for item in originals}

        # Never silently accept an output line outside this batch. It can be a
        # context line, a hallucinated line number, or a one-based shift.
        unexpected = [translation for key, translation in self.translations.items() if key not in original_keys]
        if unexpected:
            self.errors.append(TranslationError(
                f"Found {len(unexpected)} translation lines outside the requested batch",
                translation=self.text))

        for item in originals:
            translation : SubtitleLine|None = self.translations.get(item.key)
            if translation:
                translation.number = item.number
                translation.start = item.start or timedelta(seconds=0)
                translation.end = item.end or timedelta(seconds=0)
                translation.metadata = item.metadata

                if translation.original and IsTextContentEqual(translation.text, item.text):
                    # Check for swapped original & translation
                    translation.text = translation.original
                    translation.original = item.text

                item.translation = translation.text
                matched.append(translation)

                # An exact copy of a different source cue is a strong signal
                # that the model echoed context or shifted its output. Keep it
                # visible for diagnostics, but force the batch into retry.
                for other in originals:
                    if other.key != item.key and IsTextContentEqual(translation.text, other.text):
                        self.errors.append(TranslationError(
                            f"Translation for line {item.number} echoes source line {other.number}",
                            translation=self.text))
                        break

            else:
                item.translation = None
                unmatched.append(item)

        if unmatched:
            self.TryFuzzyMatches(unmatched)

        if unmatched:
            # Fuzzy matching is diagnostic only. Never treat a guessed mapping
            # as complete, because a shifted line can otherwise ship silently.
            self.errors.append(UntranslatedLinesError(f"No translation found for {len(unmatched)} lines", lines=unmatched))

        self._detect_one_based_shift(originals)
        return matched, unmatched

    def _detect_one_based_shift(self, originals: list[SubtitleLine]) -> None:
        """Reject a complete response whose line numbers are shifted by one."""
        if not originals or not self.translations:
            return

        original_numbers = [line.number for line in originals]
        response_numbers = list(self.translations.keys())
        if not all(isinstance(number, int) for number in response_numbers):
            return
        if len(response_numbers) != len(original_numbers):
            return

        original_set = set(original_numbers)
        response_set = set(response_numbers)
        shifted_up = {number + 1 for number in original_numbers}
        shifted_down = {number - 1 for number in original_numbers}
        if response_set == shifted_up or response_set == shifted_down:
            self.errors.append(TranslationError(
                "Translation line numbers appear to be shifted by one",
                translation=self.text))

    def TryFuzzyMatches(self, unmatched : list [SubtitleLine]) -> None:
        """
        Try to match translations to their source lines using heuristics
        """
        possible_matches : list[tuple[SubtitleLine,SubtitleLine]] = []
        for item in (item for item in unmatched if item.number is not None):
            for translation in self.translations.values():
                if translation.original:
                    if IsTextContentEqual(translation.original, item.text):
                        # A match on the original text is pretty compelling
                        possible_matches.append((item, translation))
                        continue
                    elif IsTextContentEqual(translation.text, item.text):
                        # LLMs sometimes swap the original and translated text - swap them back
                        translation.text = translation.original
                        translation.original = item.text
                        possible_matches.append((item, translation))
                        continue

                    #TODO: check for merged lines

        if possible_matches:
            for item, translation in possible_matches:
                self.warnings.append(_("Found fuzzy match for line {number} in translations").format(number=item.number))
                item.translation = f"#Fuzzy: {translation.text}"
                #unmatched.remove(item)

    def ValidateTranslations(self) -> list[Exception]:
        """
        Check if the translation seems at least plausible
        """
        if not self.translated:
            return [ NoTranslationError("Failed to extract translations from response", translation=self.text) ]

        validator = SubtitleValidator(self.options)
        return validator.ValidateTranslations(self.translated)

    def _fix_unclosed_tags(self):
        """
        Check if the last line of the translation picked up a summary without a closing tag
        """
        last_line : SubtitleLine = self.translated[-1]

        if not last_line.text:
            return

        # Use a regex to check for opening metatags and ensure there is a matching close tag. If not, truncate the text at the tag.
        re_opening = regex.compile(rf"<({'|'.join(self.metatags)})>", regex.IGNORECASE)

        for match in re_opening.finditer(last_line.text):
            tag = match.group(1)
            if not regex.search(rf"</{tag}>", last_line.text):
                self.warnings.append(_("Found unclosed tag {tag} in translation").format(tag=tag))
                last_line.text = last_line.text[:match.start()]
                break
            
