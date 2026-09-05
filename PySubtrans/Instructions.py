DEFAULT_TASK_TYPE = "Translation"

default_user_prompt = "Translate these subtitles [ for movie][ to language]"

linesep = '\n'

default_instructions = linesep.join([
    "The goal is to accurately translate subtitles into a target language.",
    "",
    "You will receive a batch of lines for translation. Carefully read through the lines, along with any additional context provided.",
    "First understand the meaning, emotional intent, speaker relationship, and sentence continuity of the entire batch and its nearby dialogue context.",
    "Translate naturally and idiomatically for the target audience, rather than word-for-word. Preserve the speaker's personality, register, humor, tension, subtext, and emotional force.",
    "When a sentence is split across subtitle cues, mentally translate the complete sentence and then divide the result naturally across the corresponding cues. Do not repeat words or duplicate a sentence across cues.",
    "",
    "The translation must have the same number of lines as the original, but you can adapt the content to fit the grammar of the target language.",
    "Keep subtitle lines concise and comfortable to read. Prefer natural spoken language over formal written language unless the context clearly requires formality.",
    "Respect established names, terminology, pronouns, forms of address, and character voice from the supplied context. A user-provided glossary always takes precedence over inferred terminology.",
    "Preserve all subtitle markup, formatting tags, speaker labels, line breaks, and non-dialogue symbols exactly unless the context explicitly says otherwise. Never translate tag names or formatting codes.",
    "Do not add information that is not implied by the source or context.",
    "",
    "The translation must have the same number of lines as the original, but you can adapt the content to fit the grammar of the target language.",
    "Make sure to translate all provided lines and do not ask whether to continue.",
    "",
    "Use any provided context to enhance your translations. If a name list is provided, ensure names are spelled according to the user's preference.",
    "If you detect obvious errors in the input, correct them in the translation using the available context, but do not improvise.",
    "If the input contains profanity, use equivalent profanity in the translation.",
    "",
    "At the end you should add <summary> and <scene> tags with information about the translation:",
    "<summary>A one or two line synopsis of the current batch.</summary>",
    "<scene>This should be a short summary of the current scene, including any previous batches.</scene>",
    "If the context is unclear, just summarize the dialogue.",
    "",
    "Your response will be processed by an automated system, so you MUST respond using the required format:",
    "",
    "Example (translating to English):",
    "",
    "#200",
    "Original>",
    "変わりゆく時代において、",
    "Translation>",
    "In an ever-changing era,",
    "",
    "#501",
    "Original>",
    "進化し続けることが生き残る秘訣です。",
    "Translation>",
    "continuing to evolve is the key to survival.",
    ])

default_vietnamese_instructions = linesep.join([
    "Vietnamese localization rules:",
    "Use natural spoken Vietnamese suitable for subtitles, not word-for-word translation or stiff written prose.",
    "Choose Vietnamese pronouns and forms of address from the relationship, age, status, intimacy, conflict, and scene context.",
    "Keep each character's pronouns and speech register consistent across the whole story unless the relationship clearly changes.",
    "Prefer concise, idiomatic Vietnamese phrases. Preserve implications, sarcasm, politeness, insults, humor, and emotional subtext.",
    "Do not force English sentence order into Vietnamese. Reorder clauses when needed for natural Vietnamese grammar.",
    "Do not add chủ ngữ when Vietnamese naturally omits it, and do not add pronouns merely because the source repeats them.",
    "Use Vietnamese punctuation naturally, while preserving subtitle markup and cue boundaries.",
])

default_terminology_instructions = linesep.join([
    "If a terminology reference is provided, use those translations consistently.",
    "",
    "After translation, add a <terminology> block listing any terminology from the source subtitles that require a consistent translation,",
    "e.g. character names, nicknames, titles, organisations, locations, unique objects, or uncommon cultural and technical concepts.",
    "",
    "Format each entry as one 'original::translation' pair per line, e.g.",
    "<terminology>",
    "Source Language::目標語言",
    "</terminology>",
    ])

default_polish_instructions = linesep.join([
    "You are reviewing an existing Vietnamese subtitle translation for naturalness.",
    "Improve only fluency, idiomatic Vietnamese, character voice, pronoun consistency, emotional nuance, and subtitle readability.",
    "Do not change the meaning, add information, remove meaning, or rewrite the scene freely.",
    "Keep exactly the same number of subtitle lines and exactly the same line numbers.",
    "Do not merge, split, reorder, or omit lines. Preserve every markup token exactly.",
    "Respect the supplied terminology and forms of address. Return only the required numbered translation format.",
])

default_retry_instructions = linesep.join([
	"There was an issue with the previous translation.",
	"",
	"Translate the subtitles again, ensuring each line is translated SEPARATELY, and EVERY line has a corresponding translation.",
	"",
	"Do NOT merge lines together in the translation, it leads to incorrect timings and confusion for the reader."
    ])

class Instructions:
    def __init__(self, settings : dict) -> None:
        self.prompt : str|None = None
        self.instructions : str|None = None
        self.retry_instructions : str|None = None
        self.polish_instructions : str|None = None
        self.terminology_instructions : str|None = None
        self.instruction_file : str|None = None
        self.target_language : str|None = None
        self.task_type : str|None = DEFAULT_TASK_TYPE
        self.InitialiseInstructions(settings)

    def GetSettings(self) -> dict[str, str|None]:
        """ Generate the settings for these instructions """
        settings = {
            'prompt': self.prompt,
            'instructions': self.instructions,
            'retry_instructions': self.retry_instructions,
            'polish_instructions': self.polish_instructions,
            'terminology_instructions': self.terminology_instructions,
            'instruction_file': self.instruction_file,
            'task_type' : self.task_type
        }

        if self.target_language:
            settings['target_language'] = self.target_language

        return settings

    def InitialiseInstructions(self, settings : dict[str, str|None]) -> None:
        self.prompt = settings.get('prompt') or default_user_prompt
        self.instructions = settings.get('instructions') or default_instructions
        self.retry_instructions = settings.get('retry_instructions') or default_retry_instructions
        self.polish_instructions = settings.get('polish_instructions') or default_polish_instructions
        self.terminology_instructions = settings.get('terminology_instructions') or default_terminology_instructions
        self.instruction_file = settings.get('instruction_file')
        self.target_language = None
        self.task_type = settings.get('task_type') or DEFAULT_TASK_TYPE

        # Add any additional instructions from the command line
        if settings.get('instruction_args') and isinstance(settings['instruction_args'], list):
            additional_instructions = linesep.join(settings['instruction_args'])
            if additional_instructions:
                self.instructions = linesep.join([self.instructions, additional_instructions])

        tags = {
            "[ for movie]": f" for {settings.get('movie_name')}" if settings.get('movie_name') else "",
            "[ to language]": f" to {settings.get('to_language')}" if settings.get('to_language') else "",
        }

        tags.update({ f"[{k}]": v for k, v in settings.items() if v })

        self.prompt = ReplaceTags(self.prompt, tags)
        self.instructions = ReplaceTags(self.instructions, tags)
        self.retry_instructions = ReplaceTags(self.retry_instructions, tags)

def IsVietnameseLanguage(language: str) -> bool:
    """Return whether a language setting identifies Vietnamese."""
    normalized = language.strip().casefold()
    return normalized in {'vi', 'vie', 'vietnamese', 'tiếng việt', 'tieng viet'} or 'vietnam' in normalized


def ReplaceTags(text : str, tags : dict[str, str]) -> str:
    """
    Replace option tags in a string with the value of the corresponding option.
    """
    if text:
        for name, value in tags.items():
            if value:
                text = text.replace(f"[{name}]", str(value))
    return text
