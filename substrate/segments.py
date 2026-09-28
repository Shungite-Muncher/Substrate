"""Market segments, the public companies that anchor each one, and the
vocabulary used to route trade-press stories to them."""
from __future__ import annotations

import re

SEGMENTS: dict[str, dict] = {
    "logic": {
        "label": "Processors & advanced logic",
        "companies": ["NVDA", "AMD", "INTC"],
        "foundry_exposure": 1.0,   # how strongly TSMC loading matters
        "keywords": ["tsmc", "foundry", "wafer", "cowos", "advanced packaging", "3nm", "2nm", "n3", "n2",
                     "gpu", "cpu", "processor", "soc", "asic", "nvidia", "amd", "intel", "logic"],
    },
    "memory": {
        "label": "Memory (DRAM / NAND / HBM)",
        "companies": ["MU"],
        "foundry_exposure": 0.2,
        "keywords": ["dram", "nand", "hbm", "ddr5", "ddr4", "lpddr", "memory", "flash", "ssd", "micron",
                     "sk hynix", "hynix", "samsung memory", "kioxia", "western digital", "sandisk"],
    },
    "analog": {
        "label": "Analog & power management",
        "companies": ["TXN", "ADI"],
        "foundry_exposure": 0.3,
        "keywords": ["analog", "pmic", "power management", "data converter", "adc", "dac", "op amp",
                     "texas instruments", "analog devices", "ldo", "regulator", "interface ic"],
    },
    "mcu": {
        "label": "Microcontrollers & embedded",
        "companies": ["MCHP", "NXPI"],
        "foundry_exposure": 0.6,
        "keywords": ["microcontroller", "mcu", "embedded", "microchip", "nxp", "renesas", "stmicro",
                     "stm32", "infineon", "automotive chip", "mature node", "28nm", "40nm"],
    },
    "power": {
        "label": "Power discretes (MOSFET / IGBT / SiC / GaN)",
        "companies": ["ON"],
        "foundry_exposure": 0.2,
        "keywords": ["mosfet", "igbt", "sic", "silicon carbide", "gan", "gallium nitride", "discrete",
                     "onsemi", "wolfspeed", "power semiconductor", "diode", "rectifier"],
    },
}

# Distributors: the customer's peer set when the customer is a distributor.
DISTRIBUTORS = {"ARW": "Arrow Electronics", "AVT": "Avnet"}

COMPANY_NAMES = {
    "NVDA": "NVIDIA", "AMD": "AMD", "INTC": "Intel", "MU": "Micron", "TXN": "Texas Instruments",
    "ADI": "Analog Devices", "MCHP": "Microchip", "NXPI": "NXP", "ON": "onsemi", **DISTRIBUTORS,
}

# Octopart / distributor category names -> segment
CATEGORY_HINTS = [
    (r"memory|dram|sdram|flash|eeprom|sram|nand", "memory"),
    (r"microcontroller|mcu|embedded - micro", "mcu"),
    (r"mosfet|igbt|transistor|diode|rectifier|thyristor|discrete|sic|gan", "power"),
    (r"regulator|pmic|power management|amplifier|op amp|data acquisition|adc|dac|analog|interface|driver|reference",
     "analog"),
    (r"fpga|processor|cpu|gpu|dsp|soc|logic", "logic"),
]


def segment_for_category(text: str | None) -> str | None:
    if not text:
        return None
    t = text.lower()
    for pat, seg in CATEGORY_HINTS:
        if re.search(pat, t):
            return seg
    return None


def segments_for_text(text: str) -> list[str]:
    t = f" {text.lower()} "
    hits = []
    for seg, spec in SEGMENTS.items():
        if any(re.search(rf"(?<![a-z0-9]){re.escape(k)}(?![a-z0-9])", t) for k in spec["keywords"]):
            hits.append(seg)
    return hits


def all_tickers() -> list[str]:
    out = []
    for spec in SEGMENTS.values():
        out += spec["companies"]
    return out + list(DISTRIBUTORS)
