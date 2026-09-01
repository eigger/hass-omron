"""Model Number Strings that need mapping to a catalog id.

A cuff answers the GATT Model Number String with whatever its carton says, and
that is often neither the profile key nor one of the variants already listed:
a BP5465 is a HEM-7382T1-AZAZ, and a cuff calling itself HEM-7140T1 is really a
HEM-7140T1-AP. Without a mapping such a name matches nothing and falls back to
the default profile, which then reads the wrong EEPROM layout.

Taken from the OMRON connect Android app's own device list, which carries both
names for every device it supports, and kept only where the target already
resolves in this catalog.

Three names are deliberately absent. BP5350, BP7350 and BP7350CAN each cover two
hardware revisions that speak different protocols -- HEM-7155T is WLS3.0 with
four RX/TX channels and custom-key pairing, HEM-7155T-K4 is WLD2.0 with one
channel and OS bonding -- so a wrong guess reads the device through the wrong
stack entirely. The app separates them too, by a group id that lands in a range
this catalog already classifies the same way, so the split is real rather than
an artefact of how these profiles were once divided here.

They are resolvable in principle: the two stacks expose different parent service
UUIDs, which is visible on connect. Doing that needs the device in hand rather
than a model string, so it is left for later.

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
}
