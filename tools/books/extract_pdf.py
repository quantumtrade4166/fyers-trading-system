r"""Extract a PDF's text page by page (for book strategy extraction).
    .venv\Scripts\python.exe tools\books\extract_pdf.py "<pdf>" "<out.txt>"
Prints pages that have little/no text (scanned images need OCR)."""
import sys, logging, warnings
sys.stdout.reconfigure(encoding="utf-8"); logging.disable(50); warnings.filterwarnings("ignore")
from pypdf import PdfReader
r = PdfReader(sys.argv[1])
low = []
with open(sys.argv[2], "w", encoding="utf-8") as out:
    for i, p in enumerate(r.pages, 1):
        t = p.extract_text() or ""
        if len(t.strip()) < 80:
            low.append(i)
        out.write(f"\n=== PAGE {i} ===\n{t}")
print(f"{len(r.pages)} pages | pages with <80 chars of text: {len(low)} {low[:40]}")
