"""callsheet.NOTE_BANNED_PHRASES entries for the 'minority stake purchase' and 'role vacated' notes (synthetic text only)."""
import pytest

from src import callsheet as cs
from src import signals as sig

MINORITY = sig.MA_TARGET_MINORITY_NOTE
VACATED = sig.VACATED_NOTE


def test_minority_stake_purchase_bans_takeover_language():
    banned = cs._banned_for([MINORITY], "Acme to sell a 9.9% stake to Bigco.")
    for phrase in ("takeover", "being acquired", "acquisition of the company", "full acquisition", "acquired by", "buyout"):
        assert phrase in banned


def test_role_vacated_bans_claims_that_a_replacement_exists():
    banned = cs._banned_for([VACATED], "Acme's CTO has left to join another organisation.")
    for phrase in ("has appointed", "appointed a new", "successor", "replacement", "has hired", "new head", "new chief",
                   "congrats on the new role", "congratulations on the new role", "welcome the new"):
        assert phrase in banned


def test_phrases_the_scoop_itself_says_are_left_out():
    ev = "Acme's CTO has left; a successor has not been named and a replacement is being sought."
    banned = cs._banned_for([VACATED], ev)
    assert "successor" not in banned and "replacement" not in banned and "has appointed" in banned
    ev2 = "Bigco is seeking a full acquisition of a minority holder."
    assert "full acquisition" not in cs._banned_for([MINORITY], ev2) and "takeover" in cs._banned_for([MINORITY], ev2)


@pytest.mark.parametrize("note", [MINORITY, VACATED])
def test_every_new_note_string_matches_a_banned_phrase_key(note):
    assert any(note.startswith(k) for k in cs.NOTE_BANNED_PHRASES)


def test_existing_entries_unchanged():
    assert cs.NOTE_BANNED_PHRASES["acquirer of a minority stake"] == ("acquired", "takeover", "acquisition of the company",
                                                                       "full acquisition")
    assert "debt facility" in cs.NOTE_BANNED_PHRASES and "reported talks" in cs.NOTE_BANNED_PHRASES


def test_other_notes_add_nothing():
    assert cs._banned_for([sig.MA_TARGET_NOTE], "Acme to be acquired by Bigco.") == []


def test_notes_combine_without_duplicates():
    banned = cs._banned_for([MINORITY, sig.MA_MINORITY_NOTE], "Plain text.")
    assert len(banned) == len(set(banned)) and "takeover" in banned
