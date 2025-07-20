import json
from core.integration_loader import load_all_integrations

fingerprints = []
for integration in load_all_integrations():
    for fp in integration.fingerprints:
        fingerprints.append({
            "integration": integration.name,
            "match": fp
        })

with open("runtime_fingerprint_index.json", "w") as f:
    json.dump(fingerprints, f, indent=2) 