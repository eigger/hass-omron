"""Model Number Strings that need mapping to a catalog id.

A cuff answers the GATT Model Number String with whatever its carton says, and
that is often neither the profile key nor one of the variants already listed:
a BP5465 is a HEM-7382T1-AZAZ, and a cuff calling itself HEM-7140T1 is really a
HEM-7140T1-AP. Without a mapping such a name matches nothing and falls back to
the default profile, which then reads the wrong EEPROM layout.

Taken from the OMRON connect Android app's own device list, which carries three
names for every device it supports -- an identifier (HEM-7600T-Z), a Bluetooth
settings name (BP7000) and a display name (Evolv) -- and kept only where the
target already resolves in this catalog. Firmware answers with any of the
three.

Some names cannot be mapped at all, and those live in AMBIGUOUS_MODEL_NAMES.
The app ships HEM-7155T_ESL and HEM-7155T_K4-ESL with all three names identical
-- WLS3.0 with custom-key pairing against WLD2.0 with OS bonding -- and tells
them apart by a group id the device never transmits. It knows which is which
only because the user picked the model, and so must we.

Resolvable in principle: the two stacks expose different parent service UUIDs,
visible on connect. That needs the device in hand, so it is left for later.

Resolution aliases only: get_supported_models does not offer them in the
config-flow dropdown, which stays on the HEM designations.
"""
from __future__ import annotations

MODEL_NUMBER_ALIASES: dict[str, str] = {
    "BP300": "HEM-7320T-ZV",
    "BP4350": "HEM-6232T-Z",
    "BP5150": "HEM-7142T2-ZAZ",
    "BP5250": "HEM-7151T-Z",
    "BP5255": "HEM-716BT2-ZAZ",
    "BP5360": "HEM-7377T1-ZAZ",  # WLD3.0/4.0 family
    "BP5450": "HEM-7343T-Z",
    "BP5465": "HEM-7382T1-AZAZ",  # WLD3.0/4.0 family
    "BP6001": "HEM-6402T-Z",
    "BP6350": "HEM-6231T_Z",
    "BP653": "HEM-6321T-Z",
    "BP654": "HEM-6320T-Z",
    "BP7000": "HEM-7600T-Z",
    "BP7150": "HEM-7142T2-Z",
    "BP7250": "HEM-7150T-Z",
    "BP7250CAN": "HEM-7150T-CA",
    "BP7255": "HEM-716CT2-Z",
    "BP7360": "HEM-7376T1-Z",  # WLD3.0/4.0 family
    "BP7365CAN": "HEM-7376T1-ACACD6",  # WLD3.0/4.0 family
    "BP7450": "HEM-7342T-Z",
    "BP7450CAN": "HEM-7342T-CA",
    "BP7455CAN": "HEM-7342T1-ACACD6",
    "BP7465": "HEM-7381T1-AZ",  # WLD3.0/4.0 family
    "BP761CANN": "HEM-7320T-CA",
    "BP761N": "HEM-7320T-ZV",
    "BP761|CAN": "HEM-7320T_TI-Z",
    "BP769CAN": "HEM-7320T-CACS",
    "BP786CANN": "HEM-7321T-CA",
    "BP786N": "HEM-7321T-ZV",
    "BP786|CAN": "HEM-7321T_TI-Z",
    "BP7900": "HEM-7530T-Z",
    "BP8000": "HEM-6410T-Z",
    "HCR-1901T2": "HEM-1026T2-AJC",
    "HCR-1902T2": "HEM-1026T2-AJE",
    "HCR-7206T2": "HEM-7146T2-JD",
    "HCR-7308T2": "HEM-7146T2-JF",
    "HCR-7608T2": "HEM-7600T2-JF",
    "HCR-7612T2": "HEM-7346T2-AJE32",
    "HCR-761AT2": "HEM-7346T2-AJC32",
    "HCR-7711T2": "HEM-7347T2-AJC32",
    "HCR-7712T2": "HEM-7347T2-AJE32",
    "HEM-6161T-D/E": "HEM-6161T-E",
    "HEM-6232T-D/E": "HEM-6232T-D",
    "HEM-7140T1": "HEM-7140T1-AP",
    "HEM-7142T1": "HEM-7142T1-AP",
    "HEM-7143T1-EBK": "HEM-7143T1-E",
    "HEM-7143T2-ESL": "HEM-7143T2-E",
    "HEM-7155T-AP": "HEM-7155T_AP",
    "HEM-7156T": "HEM-7156T_AP",
    "HEM-7156T-AAP": "HEM-7156T_AAP",
    "HEM-9601T-E3": "HEM-9601T_E3",
    # Marketing and series names, which some firmware answers with
    # instead of a code -- a HEM-7188T1-LEO reports "X2+ Connect".
    "10 Series Wrist": "HEM-6321T-Z",
    "3 Series Upper Arm": "HEM-7142T2-Z",
    "Blood Pressure Monitor / Tensiomètre": "HEM-7376T1-ACACD6",
    "Boots Blood Pressure device": "HEM-7361T1-BS",
    "Bronze Upper Arm": "HEM-7142T2-ZAZ",
    "Complete": "HEM-7530T_E3",
    "Complete(HEM-7530T1)": "HEM-7530T1-BR3",
    "EVOLV": "HEM-7600T-E",
    "Evolv": "HEM-7600T-Z",
    "HCR-7501T": "HEM-7158T-JC",
    "HCR-7502T": "HEM-7346T-AJE3",
    "HCR-750AT": "HEM-7346T-AJC3",
    "HCR-7601T": "HEM-7347T-AJC3",
    "HCR-7602T": "HEM-7347T-AJE3",
    "HCR-7800T": "HEM-7530T_J3",
    "HEM-7141T1": "HEM-7141T1-AP",
    "HEM-7143T1": "HEM-7143T1-AP",
    "HEM-7143T2": "HEM-7143T2-E",
    "HEM-7144T1": "HEM-7144T1-AU",
    "HEM-7156T-A": "HEM-7156T_AAP",
    "HEM-7280T": "HEM-7280T-AP",
    "HeartGuide": "HEM-6410T-Z",
    "HeartVue": "HEM-6402T-Z",
    "JPN610T": "HEM-7158T_AP3",
    "JPN616T": "HEM-7159T_AP3",
    "JPN710T": "HEM-7346T_AP3",
    "M2 Intelli IT": "HEM-7143T1-E",
    "M2 Intelli IT+": "HEM-7146T2-EBK",
    "M2+": "HEM-7188T1-LE",
    "M300 Intelli IT": "HEM-7143T1-D",
    "M4 Connect AFib": "HEM-7196T1-FLE",
    "M500 Intelli IT": "HEM-7361T-D",
    "M7 Intelli IT AFib": "HEM-7380T1-EBK",
    "M700 Intelli IT": "HEM-7322T-D",
    "MIT5s": "HEM-7280T-E",
    "NightView": "HEM-9601T_E3",
    "OMRON Complete": "HEM-7530T-Z",
    "OMRON upper arm blood pressure monitor": "HEM-7600T-ZCD6BK",
    "Platinum": "HEM-7343T-Z",
    "RS3 Intelli IT": "HEM-6161T-E",
    "RS7 Intelli IT": "HEM-6232T-D",
    "Wrist Blood Pressure Monitor": "HEM-6232T-Z",
    "X2 Smart": "HEM-7143T2-E",
    "X2 Smart+": "HEM-7146T2-ESL",
    "X2+": "HEM-7188T1-LEO",
    "X4 Connect AFib": "HEM-7196T1-FLEO",
    "X7 Smart": "HEM-7361T_ESL",
    "X7 Smart AFib": "HEM-7380T1-EOSL",
}


AMBIGUOUS_MODEL_NAMES: dict[str, tuple[str, ...]] = {
    "10 Series Upper Arm": (
        "HEM-7321T-CA",
        "HEM-7321T-ZV",
        "HEM-7321T_TI-Z",
        "HEM-7342T-CA",
        "HEM-7342T-Z",
        "HEM-7342T1-ACACD6",
        "HEM-7381T1-AZ",
        "HEM-7382T1-AZAZ",
    ),
    "5 Series Upper Arm": (
        "HEM-7150T-CA",
        "HEM-7150T-Z",
        "HEM-716CT2-Z",
    ),
    "7 Series Upper Arm": (
        "HEM-7320T-CA",
        "HEM-7320T-CACS",
        "HEM-7320T-ZV",
        "HEM-7320T_TI-Z",
        "HEM-7340T-CA",
        "HEM-7340T-Z",
        "HEM-7340T_K4-CA",
        "HEM-7340T_K4-Z",
        "HEM-7376T1-Z",
        "HEM-7377T1-ZAZ",
    ),
    "7 Series Wrist": (
        "HEM-6231T_Z",
        "HEM-6320T-Z",
    ),
    "BP5350": (
        "HEM-7341T-Z",
        "HEM-7341T_K4-Z",
    ),
    "BP7350": (
        "HEM-7340T-Z",
        "HEM-7340T_K4-Z",
    ),
    "BP7350CAN": (
        "HEM-7340T-CA",
        "HEM-7340T_K4-CA",
    ),
    "Gold": (
        "HEM-7341T-Z",
        "HEM-7341T_K4-Z",
    ),
    "HEM-7155T-D": (
        "HEM-7155T-D",
        "HEM-7155T_K4-D",
    ),
    "HEM-7155T-EBK": (
        "HEM-7155T-EBK",
        "HEM-7155T_K4-EBK",
    ),
    "HEM-7155T_ESL": (
        "HEM-7155T_ESL",
        "HEM-7155T_K4-ESL",
    ),
    "M4 Intelli IT": (
        "HEM-7155T-EBK",
        "HEM-7155T_K4-EBK",
    ),
    "M400 Intelli IT": (
        "HEM-7155T-D",
        "HEM-7155T_K4-D",
    ),
    "M7 Intelli IT": (
        "HEM-7322T-E",
        "HEM-7361T-EBK",
    ),
    "Silver": (
        "HEM-7151T-Z",
        "HEM-716BT2-ZAZ",
    ),
    "X4 Smart": (
        "HEM-7155T_ESL",
        "HEM-7155T_K4-ESL",
    ),
}
