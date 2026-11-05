"""PII masking before cloud AI models: IDs, IBANs, phones and e-mails masked, values restored, no page images."""
import io

import pytest

from conftest import make_config
from docauto.models import ExtractedField, FieldDef, FieldSchema
from docauto.pii import PiiMasker
from docauto.pipeline import DocumentService, ProcessOptions
from docauto.provider import Capability, ExtractionProvider, ProviderInfo

IBAN, NID, IQAMA, EMAIL, PHONE = "SA0380000000608010167519", "1012345678", "2398765432", "ahmed.ali@example.com", "+966 55 123 4567"
LINES = ["ACME Trading Co. LLC", "Tax Invoice", "Invoice Number: INV-2026-0042", "Invoice Date: 15/10/2026",
         "Supplier VAT No: 300123456700003", f"IBAN: {IBAN}", f"National ID: {NID}", f"Iqama: {IQAMA}",
         f"Email: {EMAIL}", f"Mobile: {PHONE}", "Landline: 011 234 5678", "Total: 11,500.00 SAR"]


def test_masks_saudi_ids_ibans_phones_and_emails_and_restores_them():
    m = PiiMasker()
    text = m.mask("\n".join(LINES))
    for secret in (IBAN, NID, IQAMA, EMAIL, PHONE, "011 234 5678"):
        assert secret not in text
    assert "[IBAN_1]" in text and "[EMAIL_1]" in text and "[ID_1]" in text and "[ID_2]" in text
    # business numbers are left alone: VAT number (15 digits), invoice number, dates, amounts
    for keep in ("300123456700003", "INV-2026-0042", "15/10/2026", "11,500.00"):
        assert keep in text
    assert m.unmask(text) == "\n".join(LINES)
    assert m.mask(f"again {IBAN}") == "again [IBAN_1]"                         # same value, same placeholder
    assert m.unmask({"a": ["[PHONE_1]", "ID_2"]}) == {"a": [PHONE, IQAMA]}       # brackets dropped by a model
    assert PiiMasker().mask("GB82 WEST 1234 5698 7654 32") == "[IBAN_1]"         # any IBAN with a valid checksum
    assert PiiMasker().mask("PO12345678901234") == "PO12345678901234"            # not an IBAN (checksum)


def _pdf(lines):
    from reportlab.lib.pagesizes import A4
    from reportlab.pdfgen import canvas
    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=A4)
    y = 800
    for t in lines:
        c.setFont("Helvetica", 12)
        c.drawString(60, y, t)
        y -= 22
    c.showPage()
    c.save()
    return buf.getvalue()


SCHEMA = FieldSchema(name="Payment details", fields=[
    FieldDef(name="iban"), FieldDef(name="national_id"), FieldDef(name="email"), FieldDef(name="mobile"),
    FieldDef(name="invoice_number")])


class FakeAI(ExtractionProvider):
    """Reads 'Label: value' lines of what it is sent (as a cloud model would) and records the request."""

    def __init__(self):
        self.requests = []

    def extract(self, req):
        self.requests.append(req)
        vals = dict(l.split(": ", 1) for l in req.document.text.splitlines() if ": " in l)
        pick = {"iban": "IBAN", "national_id": "National ID", "email": "Email", "mobile": "Mobile",
                "invoice_number": "Invoice Number"}
        return [ExtractedField(name=n, value=vals.get(k), rawText=vals.get(k), confidence=0.97, providerId=self.info.id)
                for n, k in pick.items()]


def _service(runs_locally):
    pytest.importorskip("reportlab")
    svc = DocumentService(make_config())
    svc.policy.cloud_opt_in = True
    fake = FakeAI()
    svc.registry.register(fake, ProviderInfo(
        id="fake-cloud", displayName="", version="1", vendor="", tier=3, kind="extraction",
        capabilities=[Capability.EXTRACT_SCHEMA], languages=["*"], runsLocally=runs_locally,
        creditCostPerPage={"extract_schema": 20}, optionPack="tier3_ai"), {"config": {}})
    return svc, fake


def _run(svc, **opt):
    return svc.process_bytes(_pdf(LINES), "pay.pdf", ProcessOptions(schema=SCHEMA, extractionProvider="fake-cloud",
                                                                      strictProvider=True, **opt))


def test_cloud_model_gets_masked_text_and_no_images_and_values_come_back():
    svc, fake = _service(runs_locally=False)
    res = _run(svc)
    req = fake.requests[-1]
    for secret in (IBAN, NID, EMAIL, PHONE):
        assert secret not in req.document.text
    assert req.images == []                                   # an image cannot be masked: not sent
    got = {f.name: f.value for f in res.fields}
    assert got == {"iban": IBAN, "national_id": NID, "email": EMAIL, "mobile": PHONE, "invoice_number": "INV-2026-0042"}
    assert next(f for f in res.fields if f.name == "iban").grounded      # found on the real page again
    assert IBAN in res.text                                    # the stored result keeps the real text


def test_images_only_when_the_process_allows_and_masking_can_be_switched_off():
    svc, fake = _service(runs_locally=False)
    _run(svc, cloudImages=True)
    assert len(fake.requests[-1].images) == 1 and IBAN not in fake.requests[-1].document.text
    _run(svc, maskPii=False)
    assert IBAN in fake.requests[-1].document.text and len(fake.requests[-1].images) == 1
    svc.mask_pii_default = False                               # DOCAUTO_MASK_PII=false
    _run(svc)
    assert IBAN in fake.requests[-1].document.text


def test_local_models_are_never_masked():
    svc, fake = _service(runs_locally=True)
    _run(svc, maskPii=True)
    assert IBAN in fake.requests[-1].document.text and len(fake.requests[-1].images) == 1
