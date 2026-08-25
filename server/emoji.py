"""Curated emoji vocabulary. Reactions and status emoji must use one of these
shortcodes — a fixed set keeps payloads consistent and lets agents pick by
name instead of guessing codepoints. Served raw at GET /api/emoji."""
from __future__ import annotations

EMOJI: dict[str, str] = {
    # hands & people
    "thumbsup": "👍", "thumbsdown": "👎", "wave": "👋", "clap": "👏",
    "raised_hands": "🙌", "pray": "🙏", "muscle": "💪", "point_up": "☝️",
    "point_right": "👉", "ok_hand": "👌", "crossed_fingers": "🤞",
    "handshake": "🤝", "salute": "🫡", "shrug": "🤷", "facepalm": "🤦",
    "brain": "🧠", "eyes": "👀", "ear": "👂", "speaking_head": "🗣️",
    # faces
    "smile": "😄", "grin": "😁", "joy": "😂", "sweat_smile": "😅",
    "wink": "😉", "blush": "😊", "innocent": "😇", "thinking": "🤔",
    "neutral_face": "😐", "grimacing": "😬", "rolling_eyes": "🙄",
    "sob": "😭", "scream": "😱", "exploding_head": "🤯", "sunglasses": "😎",
    "nerd": "🤓", "melting_face": "🫠", "zany": "🤪", "sleeping": "😴",
    "dizzy_face": "😵‍💫", "party_face": "🥳", "heart_eyes": "😍",
    "confused": "😕", "worried": "😟", "cry": "😢", "angry": "😠",
    # status / verdicts
    "white_check_mark": "✅", "x": "❌", "warning": "⚠️", "question": "❓",
    "exclamation": "❗", "no_entry": "⛔", "heavy_plus_sign": "➕",
    "heavy_minus_sign": "➖", "hourglass": "⏳", "stopwatch": "⏱️",
    "alarm_clock": "⏰", "recycle": "♻️", "infinity": "♾️",
    "green_circle": "🟢", "yellow_circle": "🟡", "red_circle": "🔴",
    "checkered_flag": "🏁", "construction": "🚧", "sos": "🆘",
    "arrows_counterclockwise": "🔄", "fast_forward": "⏩", "pause": "⏸️",
    # objects & work
    "rocket": "🚀", "fire": "🔥", "sparkles": "✨", "tada": "🎉",
    "boom": "💥", "zap": "⚡", "star": "⭐", "bulb": "💡", "gear": "⚙️",
    "wrench": "🔧", "hammer": "🔨", "hammer_and_wrench": "🛠️",
    "nut_and_bolt": "🔩", "test_tube": "🧪", "microscope": "🔬",
    "telescope": "🔭", "satellite": "📡", "battery": "🔋", "plug": "🔌",
    "computer": "💻", "keyboard": "⌨️", "desktop": "🖥️", "printer": "🖨️",
    "floppy_disk": "💾", "cd": "💿", "package": "📦", "file_folder": "📁",
    "open_file_folder": "📂", "page_facing_up": "📄", "clipboard": "📋",
    "memo": "📝", "pencil": "✏️", "books": "📚", "book": "📖",
    "bookmark": "🔖", "link": "🔗", "paperclip": "📎", "scissors": "✂️",
    "lock": "🔒", "unlock": "🔓", "key": "🔑", "shield": "🛡️",
    "magnifying_glass": "🔍", "chart_up": "📈", "chart_down": "📉",
    "bar_chart": "📊", "calendar": "📅", "pushpin": "📌", "round_pushpin": "📍",
    "bell": "🔔", "no_bell": "🔕", "mega": "📣", "loudspeaker": "📢",
    "envelope": "✉️", "inbox": "📥", "outbox": "📤", "mailbox": "📬",
    "label": "🏷️", "moneybag": "💰", "gem": "💎", "trophy": "🏆",
    "medal": "🏅", "dart": "🎯", "game_die": "🎲", "puzzle": "🧩",
    "art": "🎨", "camera": "📷", "movie_camera": "🎥", "film": "🎞️",
    "robot": "🤖", "alien": "👽", "ghost": "👻", "skull": "💀",
    "bug": "🐛", "spider": "🕷️", "snail": "🐌", "turtle": "🐢",
    "rabbit": "🐇", "owl": "🦉", "eagle": "🦅", "octopus": "🐙",
    # nature & misc
    "seedling": "🌱", "evergreen_tree": "🌲", "palm_tree": "🌴",
    "sun": "☀️", "moon": "🌙", "cloud": "☁️", "rainbow": "🌈",
    "snowflake": "❄️", "droplet": "💧", "ocean": "🌊", "earth": "🌍",
    "comet": "☄️", "milky_way": "🌌", "mountain": "⛰️", "volcano": "🌋",
    "heart": "❤️", "orange_heart": "🧡", "yellow_heart": "💛",
    "green_heart": "💚", "blue_heart": "💙", "purple_heart": "💜",
    "black_heart": "🖤", "broken_heart": "💔", "heartbeat": "💓",
    "100": "💯", "coffee": "☕", "tea": "🍵", "pizza": "🍕",
    "cake": "🍰", "beer": "🍺", "champagne": "🍾", "popcorn": "🍿",
}

# Seed shown in "frequently used" before an actor has history.
DEFAULT_FREQUENT = ["thumbsup", "white_check_mark", "eyes", "tada", "heart", "smile"]


def is_valid_emoji(shortcode: str) -> bool:
    return shortcode in EMOJI


def emoji_suggestions(shortcode: str, n: int = 3) -> list[str]:
    """Close matches for a bad shortcode, for actionable 422s (issue #15 S6)."""
    from difflib import get_close_matches

    return get_close_matches(shortcode, EMOJI.keys(), n=n, cutoff=0.5)
