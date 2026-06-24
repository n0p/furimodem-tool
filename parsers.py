"""Pure parsers for AT command responses.

No GTK/dbus dependencies; safe to import from anywhere.
"""

import re


ACCESS_TECH = {
    0: "GSM", 1: "GSM Compact", 2: "UTRAN", 3: "GSM w/EGPRS",
    4: "UTRAN w/HSDPA", 5: "UTRAN w/HSUPA", 6: "UTRAN w/HSDPA+HSUPA",
    7: "E-UTRAN (LTE)", 8: "EC-GSM-IoT", 9: "LTE cat-M1",
    10: "LTE cat-NB1", 11: "NR (5G)", 12: "NG-RAN (5G)", 13: "NR (5G)",
}

CEREG_STATUS = {
    0: "not registered", 1: "registered", 2: "searching",
    3: "registration denied", 4: "unknown", 5: "registered, roaming",
}


def parse_cesq(text: str) -> dict:
    """Parse +CESQ/+ECSQ: <rxlev>,<ber>,<rscp>,<ecno>,<rsrq>,<rsrp>[,...].

    Some modems (e.g. MediaTek) append vendor-specific trailing values.
    The first six values follow 3GPP TS 27.007.
    """
    out: dict = {}
    m = re.search(r"\+(?:CESQ|ECSQ):\s*([\d,\-]+)", text or "")
    if not m:
        return out
    vals = m.group(1).split(",")
    std_keys = ("rxlev", "ber", "rscp", "ecno", "rsrq", "rsrp",
                "rssi", "rsrp2", "sinr")
    for i, v in enumerate(vals):
        try:
            vi = int(v)
        except ValueError:
            continue
        if i < len(std_keys):
            out[std_keys[i]] = vi
        else:
            out[f"v{i+1}"] = vi
    if "rsrq" in out and out["rsrq"] != 255:
        out["rsrq_db"] = out["rsrq"] / 2.0 - 19.5
    if "rsrp" in out and out["rsrp"] != 255:
        out["rsrp_dbm"] = out["rsrp"] - 141
    if "rssi" in out and out["rssi"] != 255:
        out["rssi_dbm"] = out["rssi"] - 110
    if "sinr" in out and out["sinr"] != 255:
        out["sinr_db"] = out["sinr"] / 2.0 - 20.0
    if "rscp" in out and out["rscp"] != 255:
        out["rscp_dbm"] = out["rscp"] - 121
    if "rxlev" in out and out["rxlev"] != 99:
        out["rxlev_dbm"] = -110 + out["rxlev"]
    return out


def parse_cereg(text: str) -> dict:
    """Parse +CEREG/+CREG/+C5GREG: <n>,<stat>[,<tac>,<ci>,<AcT>[,<cause_type>,<reject_cause>]]."""
    out: dict = {}
    m = re.search(r"\+C[5GE]?REG:\s*([\d,\"a-fA-F]+)", text or "")
    if not m:
        return out
    parts = [p.strip().strip('"') for p in m.group(1).split(",") if p.strip()]
    if len(parts) >= 1:
        try:
            out["mode"] = int(parts[0])
        except ValueError:
            pass
    if len(parts) >= 2:
        try:
            out["status"] = int(parts[1])
        except ValueError:
            pass
    if len(parts) >= 3:
        out["tac"] = parts[2]
    if len(parts) >= 4:
        out["cell_id"] = parts[3]
    if len(parts) >= 5:
        try:
            out["act"] = int(parts[4])
        except ValueError:
            pass
    return out


def parse_cops(text: str) -> dict:
    """Parse +COPS: <mode>[,<format>,<op>,<AcT>]."""
    out: dict = {}
    m = re.search(r"\+COPS:\s*([\d,\"]+)", text or "")
    if not m:
        return out
    parts = [p.strip().strip('"') for p in m.group(1).split(",") if p.strip()]
    if not parts:
        return out
    try:
        out["mode"] = int(parts[0])
    except ValueError:
        pass
    if len(parts) >= 3:
        out["operator"] = parts[2]
    if len(parts) >= 4:
        try:
            out["act"] = int(parts[3])
        except ValueError:
            pass
    return out


def parse_ecellmeas(text: str) -> dict:
    """Parse +ECELLMEAS: <rat>,<arfcn>,<pci>,<rsrp>,<rsrq>,<snr>,<cid>,<num_of_plmn>,<plmn_id>,<plmn_name>[,...]."""
    out: dict = {}
    m = re.search(r"\+ECELLMEAS:\s*(.*?)(?:\nOK|$)", text or "", re.DOTALL)
    if not m:
        return out
    parts = [p.strip().strip('"') for p in m.group(1).split(",") if p.strip()]
    if not parts:
        return out
    keys = ("rat", "arfcn", "pci", "rsrp", "rsrq", "snr", "cid", "num_plmn")
    for i, key in enumerate(keys):
        if i < len(parts):
            try:
                out[key] = int(parts[i]) if parts[i].lstrip("-").isdigit() else parts[i]
            except ValueError:
                out[key] = parts[i]
    if len(parts) > 8:
        out["plmn"] = []
        for i in range(8, len(parts), 2):
            if i + 1 < len(parts):
                out["plmn"].append((parts[i], parts[i + 1]))
    return out


def parse_ecell(text: str) -> list[dict]:
    """Parse +ECELL: <num_of_cell>,<Act>,<cell1>,<cell2>,...
    where each cell has 16 fields: <cid>,<lac_or_tac>,<mcc>,<mnc>,<psc_or_pci>,<sig1>,<sig2>,<sig1_in_dbm>,<sig2_in_dbm>,<ta>,<ext1>,<ext2>,<ext3>,<ext4>,<ext5>,<ext6>."""
    cells = []
    m = re.search(r"\+ECELL:\s*([^\n\r]+)", text or "")
    if not m:
        return cells
    raw = m.group(1).rstrip(",").strip()
    parts: list[str] = []
    i = 0
    while i < len(raw):
        if raw[i] == '"':
            j = raw.find('"', i + 1)
            if j == -1:
                break
            parts.append(raw[i + 1:j])
            i = j + 2
        else:
            j = raw.find(',', i)
            if j == -1:
                parts.append(raw[i:].strip())
                break
            parts.append(raw[i:j].strip())
            i = j + 1
    if not parts:
        return cells
    try:
        num = int(parts[0])
    except ValueError:
        return cells
    act = None
    if len(parts) > 1:
        try:
            act = int(parts[1])
        except ValueError:
            act = parts[1]
    keys = ("cid", "lac_or_tac", "mcc", "mnc", "psc_or_pci",
            "sig1", "sig2", "sig1_in_dbm", "sig2_in_dbm",
            "ta", "ext1", "ext2", "ext3", "ext4", "ext5", "ext6")
    idx = 2
    for _ in range(num):
        cell = {"act": act}
        for key in keys:
            if idx < len(parts):
                val = parts[idx].strip()
                if val == "" or val.lower() == "null":
                    cell[key] = None
                elif val.lstrip("-").isdigit():
                    try:
                        cell[key] = int(val)
                    except ValueError:
                        cell[key] = val
                else:
                    cell[key] = val
                idx += 1
        cells.append(cell)
    return cells


def parse_eimsgeo(text: str) -> dict:
    """Parse +EIMSGEO: <account_id>,<broadcast_flag>,<latitude>,<longitude>,<accurate>,<method>,<city>,<state>,<zip>,<country>,<ue_wifi_mac>,<Confidence>,<altitude>,<accuracy_semiMajorAxis>,<accuracy_semiMinorAxis>,<accuracy_verticalAxis>."""
    out: dict = {}
    m = re.search(r"\+EIMSGEO:\s*(.*?)(?:\nOK|$)", text or "", re.DOTALL)
    if not m:
        return out
    parts = [p.strip() for p in m.group(1).split(",")]
    keys = ("account_id", "broadcast_flag", "latitude", "longitude", "accurate",
            "method", "city", "state", "zip", "country", "ue_wifi_mac",
            "confidence", "altitude", "accuracy_semiMajorAxis",
            "accuracy_semiMinorAxis", "accuracy_verticalAxis")
    for i, key in enumerate(keys):
        if i < len(parts):
            out[key] = parts[i]
    return out


def strip_at_response(text: str) -> str:
    """Strip AT response prefix (+CMD: ) and trailing OK/ERROR."""
    if not text:
        return text
    text = text.replace("\r", "")
    text = re.sub(r"^\+[A-Z0-9_]+:\s*", "", text.strip())
    text = re.sub(r"\nOK\s*$", "", text)
    text = re.sub(r"\n(ERROR\s*\d*|CME ERROR:.*)\s*$", "", text)
    return text.strip()