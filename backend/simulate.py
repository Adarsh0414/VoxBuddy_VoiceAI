"""
Phase 1 PoC simulation.

Three scenarios run end to end through the real CIE + mocked ASR/MT/TTS
agents, with latency measured at each turn:

  1. Single-partner market-stall conversation: bootstrap, bystander
     rejection, reinforcement across turns (PRD §4/§7).
  2. Conversation group: a second legitimate voice (e.g. the shopkeeper's
     spouse) joins the active conversation directly, since there's a free
     slot in the (capped) partner group — and a subsequent bystander is
     correctly rejected once the group is full and nobody's absent.
  3. Group member replacement: once a group slot's occupant has been
     silent past the absence timeout, a new voice can take that slot —
     either after two confirming turns (moderate confidence) or
     immediately (overwhelming confidence).

Run with: python simulate.py
"""

from session.manager import IncomingUtterance, SessionManager


def send(session, speaker_label, text, tt, coh, target_lang="en"):
    utt = IncomingUtterance(
        speaker_label=speaker_label, text=text, target_lang=target_lang,
        turn_taking_score=tt, semantic_coherence_score=coh,
    )
    return session.handle_utterance(utt)


def report(speaker_label, result):
    d = result.decision
    translated = result.translated_text or f"(not translated — {d.notes})"
    print(f"{speaker_label:<26} {d.role.value:<10} {d.confidence:<6.2f} "
          f"{str(d.partner_switched):<7} {str(d.partner_joined):<7} "
          f"{result.latency_ms:<9.2f} {translated}")
    return d


def run():
    session = SessionManager()

    # Enroll the user's own voice BEFORE any other utterances — this is
    # what fixes the bug discovered earlier: without it, the user's own
    # speech would be evaluated as a partner candidate and could consume a
    # conversation-group slot (see PROGRESS.md / docs/vendor_decision.md).
    session.enroll_self("tourist_self")

    print("=== Scenario 1: single-partner market-stall conversation ===\n")
    print(f"{'Speaker':<26} {'Role':<10} {'Conf':<6} {'Switch':<7} {'Joined':<7} {'Lat(ms)':<9} Translated")
    print("-" * 110)

    script = [
        ("shopkeeper", "namaste, kitne ka hai yeh?", 0.9, 0.9),
        ("tourist_self", "this is 200 rupees", 0.9, 0.9),
        ("shopkeeper", "aap kahan se ho?", 0.85, 0.85),
        ("random_vendor_nearby", "aloo le lo, sasta aloo", 0.1, 0.05),
        ("shopkeeper", "accha, France se!", 0.85, 0.8),
    ]
    for speaker_label, text, tt, coh in script:
        result = send(session, speaker_label, text, tt, coh)
        d = report(speaker_label, result)
        if speaker_label == "tourist_self":
            assert d.role.value == "self", "enrolled self speech should be tagged SELF, not evaluated as a candidate"
            assert d.speaker_id not in session.state.active_partner_ids

    print("\n=== Scenario 2: a second legitimate voice joins the conversation ===\n")
    print(f"{'Speaker':<26} {'Role':<10} {'Conf':<6} {'Switch':<7} {'Joined':<7} {'Lat(ms)':<9} Translated")
    print("-" * 110)

    # Room is available (group cap is 2, currently 1 member) — a confident
    # new voice joins directly, no absence/confirmation needed for a join.
    d = report("shopkeeper_spouse",
                send(session, "shopkeeper_spouse", "aur yeh dekhiye, bahut accha hai", 0.9, 0.9))
    assert d.partner_joined is True, "expected the spouse to join the group directly"
    assert len(session.state.active_partner_ids) == 2

    # Now the group is full (2/2) and both members are recently active —
    # a bystander with the same confidence profile that would have joined
    # a moment ago is now correctly rejected: no free slot, nobody absent.
    d = report("another_random_vendor",
                send(session, "another_random_vendor", "sasta sasta, dekh lo", 0.9, 0.9))
    assert d.role.value == "bystander"
    assert "group full" in d.notes

    print("\n=== Scenario 3: replacing a group member who has gone quiet ===\n")
    print(f"{'Speaker':<26} {'Role':<10} {'Conf':<6} {'Switch':<7} {'Joined':<7} {'Lat(ms)':<9} Translated")
    print("-" * 110)

    # Simulate the spouse having been silent long enough to be replaceable.
    spouse_id = next(iter(session.state.active_partner_ids - {session.state.primary_partner_id}))
    session.state.speakers[spouse_id].last_seen -= 10

    # Moderate confidence -> needs two confirming turns from the same voice.
    d1 = report("new_customer (1st utterance)",
                 send(session, "new_customer", "excusez-moi, avez-vous ceci en bleu?", 0.9, 0.9))
    assert d1.partner_switched is False
    d2 = report("new_customer (2nd utterance)",
                 send(session, "new_customer", "avez-vous ceci en bleu?", 0.9, 0.9))
    assert d2.partner_switched is True, "expected the confirmed switch to commit on the 2nd turn"

    # Now replace the ORIGINAL shopkeeper the same way, but with
    # overwhelming confidence this time — should switch on one turn.
    shopkeeper_id = next(iter(session.state.active_partner_ids - {d2.speaker_id}))
    session.state.speakers[shopkeeper_id].last_seen -= 10
    d3 = report("very_clear_new_customer",
                 send(session, "very_clear_new_customer", "où est la sortie?", 1.0, 1.0))
    assert d3.partner_switched is True and "fast-tracked" in d3.notes

    print("\nFinal conversation state:")
    print(f"  primary_partner_id   = {session.state.primary_partner_id}")
    print(f"  active_partner_ids   = {sorted(session.state.active_partner_ids)}")
    print(f"  total speakers seen  = {len(session.state.speakers)}")
    print(f"  total turns recorded = {len(session.state.turn_history)}")


if __name__ == "__main__":
    run()
