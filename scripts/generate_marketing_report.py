"""Generates the self-contained Marketing Channel Report -- a single HTML
file with the cleaned/channel-tagged marketing data (marketing_cleaning.py)
embedded inline, and no server dependency once generated (open it directly
in a browser, or share the file with someone who has no login to this app).

This is a report generator, run on demand -- NOT the live route (that's
app.py's /marketing, which runs the exact same pipeline on every request
via marketing_cleaning.generate_report()). Re-run this script any time to
refresh the standalone file with the latest PlanetWeb data:

    python3 scripts/generate_marketing_report.py [output_path]
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import marketing_cleaning as mc

DEFAULT_OUTPUT = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "marketing_report.html")


def generate(output_path=None):
    output_path = output_path or DEFAULT_OUTPUT

    print("Running data cleaning + channel attribution pipeline...")
    html, guardrail_report = mc.generate_report(back_url=None)
    for w in guardrail_report.get("warnings", []):
        print(f"  WARNING: {w}")
    for i in guardrail_report.get("info", []):
        print(f"  info: {i}")

    with open(output_path, "w") as f:
        f.write(html)

    size_kb = os.path.getsize(output_path) / 1024
    print(f"Wrote {output_path} ({size_kb:.0f} KB)")
    return output_path


if __name__ == "__main__":
    out = sys.argv[1] if len(sys.argv) > 1 else None
    generate(out)
