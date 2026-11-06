"""Exact device model names reviewed against recent Asana PM/CM work."""

# The catalogue contains model names only. Asana task titles and device serials
# stay in the private index. New model spellings are not fuzzy substitutions.
MODEL_GROUPS = {
    "ultrasound": (
        "Affiniti 50C", "Affiniti 50G", "Affiniti 50W", "Affiniti 70C",
        "Affiniti 70W", "Affiniti CVx", "ClearVue 550", "EnVisor",
        "EPIQ 5", "EPIQ 5C", "EPIQ 5W", "EPIQ 7", "EPIQ 7C", "EPIQ CVxi",
        "HD7", "HD15", "iE33", "iU22", "Sparq",
    ),
    "patient_monitors": (
        "CM10", "CM12", "M3046A",
        "MP2", "MP5", "MP20", "MP30", "MP50", "MP70", "MP80", "MP90",
        "MX40", "MX400", "MX450", "MX500", "MX550", "MX700", "MX750",
        "MX800", "MX850", "MR200", "MR400", "VM4", "VM6", "VM8", "VS4",
        "X2", "X3",
    ),
    "fetal_monitors": ("FM20", "FM30", "FMX-4"),
    "electrocardiographs": (
        "DigiTrak XT", "PageWriter Trim III", "TC20", "TC30", "TC35",
        "TC50", "TC70",
    ),
    "defibrillators": ("DFM100", "HeartStart MRx", "HeartStart XL", "HeartStart XL+"),
    "monitoring_modules": (
        "MMX", "M1006B", "M1012A", "M1020B", "M1027A", "M1027B", "M1029A",
        "M1034B", "M1116B", "M1116C", "M2501A", "M3001A", "M3014A", "M3015A",
        "M8048A",
    ),
    "telemetry_and_central_stations": ("PIIC iX", "TRx4851A"),
}

EXACT_MATCH_MODELS = frozenset(
    model for group, models in MODEL_GROUPS.items()
    if group != "ultrasound" for model in models
)

# Only add equivalent labels confirmed by manufacturer documentation. A
# generic series name (e.g. TRx or FMX) does not stand for one specific model.
MODEL_ALIASES = {
    "M2702A": "FM20", "M2703A": "FM30", "M3002A": "X2",
    "M8001A": "MP20", "M8008A": "MP80", "M8010A": "MP90",
    "M8102A": "MP2", "M8105A": "MP5",
    "Efficia CM10": "CM10", "Efficia CM12": "CM12", "Efficia DFM100": "DFM100",
    "Avalon FM20": "FM20", "Avalon FM30": "FM30",
    "Expression MR200": "MR200", "Expression MR400": "MR400",
    "Trim III": "PageWriter Trim III",
    "MRx": "HeartStart MRx", "XL": "HeartStart XL", "XL+": "HeartStart XL+",
    "PIIC iX 866389": "PIIC iX",
    **{f"PageWriter {model}": model for model in MODEL_GROUPS["electrocardiographs"]
       if model.startswith("TC")},
    **{f"IntelliVue {model}": model for model in MODEL_GROUPS["patient_monitors"]
       if model.startswith(("MP", "MX")) or model in {"X2", "X3"}},
    **{f"SureSigns {model}": model for model in ("VM4", "VM6", "VM8", "VS4")},
}
