"""
Deterministic demo fixtures for the pre-login Interactive Demo.

These are NOT production CIE inputs and NOT a second/fake decision engine.
They are precomputed, hand-picked (speaker, signal-reading) pairs that
illustrate the five behaviors the real engine (see cie/engine.py) already
implements:

    1. partner detection      -> role PARTNER,   action TRANSLATE
    2. bystander filtering    -> role BYSTANDER, action IGNORE
    3. self-voice exclusion   -> role SELF,      action IGNORE
    4. multi-partner tracking -> role PARTNER,   action TRANSLATE (2nd slot)
    5. noise-adaptive fusion  -> role PARTNER,   action TRANSLATE (reweighted)

The signal weights below are imported from cie.engine, not redefined here,
so the demo can never drift from the actual configured CIE fusion weights
(see "Do not create a second set of decision thresholds" in the demo spec).

Nothing in this module calls ASR/translation/TTS vendors, requires
authentication, requires microphone permission, or creates a user/session/
history record. It is a static, importable data structure served read-only
by GET /api/demo/scenarios.
"""

from __future__ import annotations

from . import engine

CLEAN_WEIGHTS = {
    "voice": engine.WEIGHT_VOICE_SIMILARITY,
    "turnTaking": engine.WEIGHT_TURN_TAKING,
    "semantic": engine.WEIGHT_SEMANTIC_COHERENCE,
}

NOISY_WEIGHTS = {
    "voice": engine._NOISY_PROFILE["w_voice"],
    "turnTaking": engine._NOISY_PROFILE["w_turn"],
    "semantic": engine._NOISY_PROFILE["w_semantic"],
}

MAX_ACTIVE_PARTNERS = engine.MAX_ACTIVE_PARTNERS


# ---------------------------------------------------------------------------
# Scenario 1: Active Conversation Partner
# ---------------------------------------------------------------------------
PARTNER_DETECTION_SCENARIO = {
    "id": "partner-detection",
    "order": 1,
    "type": "partner",
    "title": "Active Conversation Partner",
    "shortTitle": "Active Partner",
    "description": "VoxBuddy recognizes a participant in the active conversation.",
    "durationMs": 7000,
    "input": {
        "speakerLabel": "Person A",
        "speechText": "Where are you going?",
        "language": "en",
        "isSimulated": True,
        "timestamp": 0,
    },
    "cie": {
        "voiceSimilarity": 0.91,
        "turnTaking": 0.84,
        "semanticCoherence": 0.88,
        "confidence": 0.89,
        "noiseLevel": 0.10,
        "noiseProfile": "clean",
        "role": "PARTNER",
        "action": "TRANSLATE",
        "weights": dict(CLEAN_WEIGHTS),
        "partnerEvent": "NONE",
        "activePartners": 1,
    },
    "outcome": {
        "role": "PARTNER",
        "action": "TRANSLATE",
        "translatedText": "\u00bfAd\u00f3nde vas?",
        "targetLanguage": "es",
        "statusMessage": "Conversation partner detected",
    },
    "explanation": (
        "CIE identified this speaker as an active conversation participant, "
        "so VoxBuddy sends the speech for translation."
    ),
    "steps": [
        {"id": "partner-input", "phase": "input", "durationMs": 1200, "state": {},
         "message": "Person A is speaking..."},
        {"id": "partner-signals", "phase": "signals", "durationMs": 2200, "state": {
            "voiceSimilarity": 0.91, "turnTaking": 0.84, "semanticCoherence": 0.88,
            "confidence": 0.89, "noiseProfile": "clean"},
         "message": "Analyzing conversation signals..."},
        {"id": "partner-decision", "phase": "decision", "durationMs": 1400, "state": {
            "role": "PARTNER", "action": "TRANSLATE", "confidence": 0.89},
         "message": "PARTNER DETECTED"},
        {"id": "partner-translation", "phase": "translation", "durationMs": 1700, "state": {
            "role": "PARTNER", "action": "TRANSLATE"},
         "message": "Translation triggered"},
        {"id": "partner-complete", "phase": "complete", "durationMs": 500, "state": {},
         "message": "Translation complete"},
    ],
}


# ---------------------------------------------------------------------------
# Scenario 2: Bystander Filtering
# ---------------------------------------------------------------------------
BYSTANDER_FILTER_SCENARIO = {
    "id": "bystander-filter",
    "order": 2,
    "type": "bystander",
    "title": "Bystander Filtering",
    "shortTitle": "Bystander",
    "description": "VoxBuddy detects nearby speech that is not part of the active conversation.",
    "durationMs": 7000,
    "input": {
        "speakerLabel": "Nearby Person",
        "speechText": "Hey, did you see the game yesterday?",
        "language": "en",
        "isSimulated": True,
        "timestamp": 0,
    },
    "cie": {
        "voiceSimilarity": 0.32,
        "turnTaking": 0.21,
        "semanticCoherence": 0.18,
        "confidence": 0.82,
        "noiseLevel": 0.18,
        "noiseProfile": "clean",
        "role": "BYSTANDER",
        "action": "IGNORE",
        "weights": dict(CLEAN_WEIGHTS),
        "partnerEvent": "NONE",
        "activePartners": 1,
    },
    "outcome": {
        "role": "BYSTANDER",
        "action": "IGNORE",
        "statusMessage": "Bystander filtered \u2014 translation skipped",
    },
    "explanation": (
        "VoxBuddy detected that this speaker is nearby but not participating "
        "in the active conversation."
    ),
    "steps": [
        {"id": "bystander-input", "phase": "input", "durationMs": 1200, "state": {},
         "message": "Nearby speech detected..."},
        {"id": "bystander-signals", "phase": "signals", "durationMs": 2200, "state": {
            "voiceSimilarity": 0.32, "turnTaking": 0.21, "semanticCoherence": 0.18,
            "confidence": 0.82},
         "message": "Checking conversation relevance..."},
        {"id": "bystander-decision", "phase": "decision", "durationMs": 1600, "state": {
            "role": "BYSTANDER", "action": "IGNORE", "confidence": 0.82},
         "message": "BYSTANDER DETECTED"},
        {"id": "bystander-complete", "phase": "complete", "durationMs": 1800, "state": {
            "role": "BYSTANDER", "action": "IGNORE"},
         "message": "Translation skipped"},
    ],
}


# ---------------------------------------------------------------------------
# Scenario 3: Self Voice Detection
# ---------------------------------------------------------------------------
SELF_DETECTION_SCENARIO = {
    "id": "self-detection",
    "order": 3,
    "type": "self",
    "title": "Your Voice",
    "shortTitle": "Self Voice",
    "description": "VoxBuddy recognizes the enrolled user's voice and prevents unnecessary translation.",
    "durationMs": 6500,
    "input": {
        "speakerLabel": "You",
        "speechText": "I think we should leave now.",
        "language": "en",
        "isSimulated": True,
        "timestamp": 0,
    },
    "cie": {
        "voiceSimilarity": 0.96,
        "turnTaking": 0.72,
        "semanticCoherence": 0.76,
        "confidence": 0.94,
        "noiseLevel": 0.08,
        "noiseProfile": "clean",
        "role": "SELF",
        "action": "IGNORE",
        "weights": dict(CLEAN_WEIGHTS),
        "selfMatch": 0.96,
        "partnerEvent": "NONE",
        "activePartners": 1,
    },
    "outcome": {
        "role": "SELF",
        "action": "IGNORE",
        "statusMessage": "Your voice detected \u2014 translation skipped",
    },
    "explanation": (
        "CIE recognized the enrolled user's voice, so VoxBuddy does not treat "
        "the user as a conversation partner."
    ),
    "steps": [
        {"id": "self-input", "phase": "input", "durationMs": 1100, "state": {},
         "message": "Your voice detected..."},
        {"id": "self-signals", "phase": "signals", "durationMs": 2100, "state": {
            "voiceSimilarity": 0.96, "turnTaking": 0.72, "semanticCoherence": 0.76,
            "confidence": 0.94, "selfMatch": 0.96},
         "message": "Comparing against enrolled voice..."},
        {"id": "self-decision", "phase": "decision", "durationMs": 1500, "state": {
            "role": "SELF", "action": "IGNORE", "confidence": 0.94, "selfMatch": 0.96},
         "message": "SELF DETECTED"},
        {"id": "self-complete", "phase": "complete", "durationMs": 1800, "state": {
            "role": "SELF", "action": "IGNORE"},
         "message": "Translation skipped"},
    ],
}


# ---------------------------------------------------------------------------
# Scenario 4: Multiple Conversation Partners
# ---------------------------------------------------------------------------
MULTI_PARTNER_SCENARIO = {
    "id": "multi-partner",
    "order": 4,
    "type": "multi_partner",
    "title": "Multiple Conversation Partners",
    "shortTitle": "Multiple Partners",
    "description": "VoxBuddy recognizes a second legitimate participant and tracks the conversation dynamically.",
    "durationMs": 9000,
    "input": {
        "speakerLabel": "Person B",
        "speechText": "We can meet there at six.",
        "language": "en",
        "isSimulated": True,
        "timestamp": 0,
    },
    "cie": {
        "voiceSimilarity": 0.87,
        "turnTaking": 0.82,
        "semanticCoherence": 0.86,
        "confidence": 0.86,
        "noiseLevel": 0.12,
        "noiseProfile": "clean",
        "role": "PARTNER",
        "action": "TRANSLATE",
        "weights": dict(CLEAN_WEIGHTS),
        "partnerEvent": "PARTNER_JOINED",
        "activePartners": 2,
    },
    "outcome": {
        "role": "PARTNER",
        "action": "TRANSLATE",
        "translatedText": "Podemos encontrarnos all\u00ed a las seis.",
        "targetLanguage": "es",
        "statusMessage": "New conversation partner detected",
    },
    "explanation": (
        "CIE recognized a second legitimate participant and added them to "
        "the active conversation."
    ),
    "steps": [
        {"id": "multi-existing", "phase": "input", "durationMs": 1000, "state": {"activePartners": 1},
         "message": "Person A is already in the conversation..."},
        {"id": "multi-new-speaker", "phase": "input", "durationMs": 1200, "state": {"activePartners": 1},
         "message": "Another speaker begins talking..."},
        {"id": "multi-signals", "phase": "signals", "durationMs": 2000, "state": {
            "voiceSimilarity": 0.87, "turnTaking": 0.82, "semanticCoherence": 0.86,
            "confidence": 0.86},
         "message": "Evaluating new speaker..."},
        {"id": "multi-join", "phase": "decision", "durationMs": 1500, "state": {
            "role": "PARTNER", "action": "TRANSLATE", "partnerEvent": "PARTNER_JOINED",
            "activePartners": 2, "confidence": 0.86},
         "message": "NEW PARTNER DETECTED"},
        {"id": "multi-translation", "phase": "translation", "durationMs": 1800, "state": {
            "role": "PARTNER", "action": "TRANSLATE"},
         "message": "Translation triggered"},
        {"id": "multi-complete", "phase": "complete", "durationMs": 500, "state": {"activePartners": 2},
         "message": "Two active partners"},
    ],
}


# ---------------------------------------------------------------------------
# Scenario 5: Noisy Environment / Adaptive Fusion
# ---------------------------------------------------------------------------
NOISE_ADAPTATION_SCENARIO = {
    "id": "noise-adaptation",
    "order": 5,
    "type": "noise",
    "title": "Noisy Environment",
    "shortTitle": "Noise Adaptation",
    "description": "CIE adapts its signal weighting when the environment becomes noisy.",
    "durationMs": 9000,
    "input": {
        "speakerLabel": "Person A",
        "speechText": "Let's take the train instead.",
        "language": "en",
        "isSimulated": True,
        "timestamp": 0,
    },
    "cie": {
        "voiceSimilarity": 0.62,
        "turnTaking": 0.86,
        "semanticCoherence": 0.91,
        "confidence": 0.83,
        "noiseLevel": 0.78,
        "noiseProfile": "noisy",
        "role": "PARTNER",
        "action": "TRANSLATE",
        "weights": dict(NOISY_WEIGHTS),
        "partnerEvent": "NONE",
        "activePartners": 1,
    },
    "outcome": {
        "role": "PARTNER",
        "action": "TRANSLATE",
        "translatedText": "Tomemos el tren en su lugar.",
        "targetLanguage": "es",
        "statusMessage": "Partner confirmed despite noisy conditions",
    },
    "explanation": (
        "In noisy conditions, CIE reduces reliance on voice similarity and "
        "gives more weight to turn-taking and semantic coherence."
    ),
    "steps": [
        {"id": "noise-clean", "phase": "input", "durationMs": 1200, "state": {
            "noiseProfile": "clean", "noiseLevel": 0.10},
         "message": "Conversation begins..."},
        {"id": "noise-detected", "phase": "signals", "durationMs": 1500, "state": {
            "noiseProfile": "noisy", "noiseLevel": 0.78},
         "message": "NOISY ENVIRONMENT DETECTED"},
        {"id": "noise-reweight", "phase": "signals", "durationMs": 1800, "state": {
            "noiseProfile": "noisy", "weights": dict(NOISY_WEIGHTS)},
         "message": "Adjusting signal fusion..."},
        {"id": "noise-decision", "phase": "decision", "durationMs": 1500, "state": {
            "voiceSimilarity": 0.62, "turnTaking": 0.86, "semanticCoherence": 0.91,
            "confidence": 0.83, "role": "PARTNER", "action": "TRANSLATE"},
         "message": "PARTNER CONFIRMED"},
        {"id": "noise-translation", "phase": "translation", "durationMs": 1700, "state": {
            "role": "PARTNER", "action": "TRANSLATE"},
         "message": "Translation triggered"},
        {"id": "noise-complete", "phase": "complete", "durationMs": 500, "state": {},
         "message": "Conversation continues"},
    ],
    # Exposed for the UI's "before/after" fusion-weight panel; sourced from
    # the same CLEAN_WEIGHTS/NOISY_WEIGHTS above, not a separate literal.
    "weightTransition": {
        "before": dict(CLEAN_WEIGHTS),
        "after": dict(NOISY_WEIGHTS),
    },
}


DEMO_SCENARIOS = [
    PARTNER_DETECTION_SCENARIO,
    BYSTANDER_FILTER_SCENARIO,
    SELF_DETECTION_SCENARIO,
    MULTI_PARTNER_SCENARIO,
    NOISE_ADAPTATION_SCENARIO,
]

DEMO_SCENARIO_SUMMARY = [
    {"id": "partner-detection", "number": 1, "label": "Active Partner", "result": "PARTNER \u2192 TRANSLATE"},
    {"id": "bystander-filter", "number": 2, "label": "Bystander", "result": "BYSTANDER \u2192 IGNORE"},
    {"id": "self-detection", "number": 3, "label": "Self Voice", "result": "SELF \u2192 IGNORE"},
    {"id": "multi-partner", "number": 4, "label": "Multiple Partners", "result": "PARTNER \u2192 TRANSLATE"},
    {"id": "noise-adaptation", "number": 5, "label": "Noise Adaptation", "result": "PARTNER \u2192 TRANSLATE"},
]
