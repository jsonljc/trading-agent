from pathlib import Path
from agent.traders.profile import load_profile


def test_pup_danny_pattern_matches_captured_author():
    p = load_profile(Path("config/traders/pup-danny.yaml"))
    assert p.discord_author_pattern == "The Pup of Wall St", (
        "If you re-renamed this, also update the audit notes; the captured "
        "Discord author for this channel is 'The Pup of Wall St', not "
        "'Pup Danny' (display_name) and not the channel slug."
    )
