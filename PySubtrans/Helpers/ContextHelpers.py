from __future__ import annotations

from typing import Any, TYPE_CHECKING

from PySubtrans.Helpers.Parse import ParseNames
from PySubtrans.SubtitleError import SubtitleError

if TYPE_CHECKING:
    from PySubtrans.Subtitles import Subtitles



def GetBatchContext(subtitles: Subtitles, scene_number: int, batch_number: int, max_lines: int|None = None) -> dict[str, Any]:
    """
    Get context for a batch of subtitles, by extracting summaries from previous scenes and batches
    """
    with subtitles.lock:
        scene = subtitles.GetScene(scene_number)
        if not scene:
            raise SubtitleError(f"Failed to find scene {scene_number}")

        batch = subtitles.GetBatch(scene_number, batch_number)
        if not batch:
            raise SubtitleError(f"Failed to find batch {batch_number} in scene {scene_number}")

        context : dict[str,Any] = {
            'scene_number': scene.number,
            'batch_number': batch.number,
            'scene': f"Scene {scene.number}: {scene.summary}" if scene.summary else f"Scene {scene.number}",
            'batch': f"Batch {batch.number}: {batch.summary}" if batch.summary else f"Batch {batch.number}"
        }

        if 'movie_name' in subtitles.settings:
            context['movie_name'] = subtitles.settings.get_str('movie_name')

        if 'description' in subtitles.settings:
            context['description'] = subtitles.settings.get_str('description')

        if 'names' in subtitles.settings:
            context['names'] = ParseNames(subtitles.settings.get('names', []))

        style_settings = {
            'style': 'translation_style',
            'tone': 'translation_tone',
            'formality': 'formality',
            'addressing_style': 'addressing_style',
            'reading_speed_guidance': 'reading_speed_guidance',
        }
        for context_key, setting_key in style_settings.items():
            value = subtitles.settings.get_str(setting_key, '')
            if value:
                context[context_key] = value

        history_lines = GetHistory(subtitles, scene_number, batch_number, max_lines)

        if history_lines:
            context['history'] = history_lines

        dialogue_limit = subtitles.settings.get_int('max_context_lines') or 50
        dialogue_context = GetDialogueContext(subtitles, scene_number, batch_number, dialogue_limit)
        if dialogue_context:
            context.update(dialogue_context)

    return context


def GetDialogueContext(subtitles: Subtitles, scene_number: int, batch_number: int, max_lines: int|None = None) -> dict[str, list[str]]:
    """Return nearby original dialogue to preserve sentence and conversation continuity."""
    if not max_lines or max_lines <= 0:
        return {}

    with subtitles.lock:
        all_lines = [line for scene in subtitles.scenes for batch in scene.batches for line in batch.originals]
        current = subtitles.GetBatch(scene_number, batch_number)
        if not current.originals:
            return {}

        first_number = current.originals[0].number
        last_number = current.originals[-1].number
        current_index = next((i for i, line in enumerate(all_lines) if line.number == first_number), None)
        if current_index is None:
            return {}

        before_count = max_lines // 2
        after_count = max_lines - before_count
        before = [line.text_normalized for line in all_lines[max(0, current_index - before_count):current_index] if line.text_normalized]
        after_start = next((i for i, line in enumerate(all_lines) if line.number == last_number), current_index) + 1
        after = [line.text_normalized for line in all_lines[after_start:after_start + after_count] if line.text_normalized]

    result: dict[str, list[str]] = {}
    if before:
        result['dialogue_before'] = before
    if after:
        result['dialogue_after'] = after
    return result


def GetHistory(subtitles: Subtitles, scene_number: int, batch_number: int, max_lines: int|None = None) -> list[str]:
    """
    Get a list of historical summaries up to a given scene and batch number
    """
    history_lines : list[str] = []
    last_summary : str = ""

    scenes = [scene for scene in subtitles.scenes if scene.number and scene.number < scene_number]
    for scene in [scene for scene in scenes if scene.summary]:
        if scene.summary != last_summary:
            history_lines.append(f"scene {scene.number}: {scene.summary}")
            last_summary = scene.summary or ""

    batches = [batch for batch in subtitles.GetScene(scene_number).batches if batch.number is not None and batch.number < batch_number]
    for batch in [batch for batch in batches if batch.summary]:
        if batch.summary != last_summary:
            history_lines.append(f"scene {batch.scene} batch {batch.number}: {batch.summary}")
            last_summary = batch.summary or ""

    if max_lines:
        history_lines = history_lines[-max_lines:]

    return history_lines
