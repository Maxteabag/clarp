"""Run: uv run --with weasyprint --with Markdown --with PyMuPDF python -m unittest discover -s skills/clarp-documents/scripts"""
import argparse
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import pymupdf
import pdf_document as pdf


class PDFDocumentTests(unittest.TestCase):
    def test_searchable_pages_links_and_local_vector(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / "diagram.svg").write_text('<svg xmlns="http://www.w3.org/2000/svg" width="300" height="60"><text x="10" y="30">Agent to attachment</text></svg>')
            source = root / "plan.md"
            source.write_text('# Product plan\n\nReadable requirements with [reference](https://example.com/spec).\n\n![Relationship](diagram.svg)\n\n<div class="page-break"></div>\n\n## Acceptance\n\nKeep durable tasks and source identity intact.\n')
            args = argparse.Namespace(source=source, title='Plan "title"', preface=None, css=None, output=root/'plan.pdf', force=False)
            result = pdf.render(args)
            self.assertEqual(result['page_count'], 2)
            self.assertFalse(any(p['outside_page'] for p in result['pages']))
            with pymupdf.open(args.output) as doc:
                self.assertIn('Readable requirements', doc[0].get_text())
                self.assertIn('Agent to attachment', doc[0].get_text())
                self.assertIn('source identity', doc[1].get_text())
                self.assertEqual(doc[0].get_links()[0]['uri'], 'https://example.com/spec')
            with self.assertRaisesRegex(ValueError, 'Output exists'):
                pdf.render(args)

    def test_missing_images_are_not_silently_published(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source = root/'plan.md'
            source.write_text('# Plan\n\nRequirements are readable but the figure is missing.\n\n![Required diagram](missing.svg)')
            args = argparse.Namespace(source=source, title='Plan', preface=None, css=None, output=root/'plan.pdf', force=False)
            with self.assertRaisesRegex(ValueError, 'missing or disallowed assets'):
                pdf.render(args)

    def test_external_and_outside_resources_rejected(self):
        fetch = pdf.local_fetcher(Path('/tmp/source'))
        for path in ['https://example.com/tracker.png', 'file:///etc/passwd', 'file://remote/tmp/source/foo']:
            with self.assertRaises(ValueError):
                fetch(path)

    def test_upload_receipt_survives_artifact_failure(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp)
            file=root/'plan.pdf'
            doc=pymupdf.open()
            page=doc.new_page()
            page.insert_text((40,40), 'A readable planning document with requirements and source identity.')
            doc.save(file)
            args=argparse.Namespace(pdf=file, receipt=root/'receipt.json', session='test-session', title='Plan', summary='Proposal')
            with patch.object(pdf.subprocess, 'check_output', side_effect=[json.dumps({'asset':{'url':'/media/test.pdf'}}), RuntimeError('ambiguous POST')]) as run:
                with self.assertRaises(RuntimeError): pdf.publish(args)
                self.assertEqual(run.call_count, 2)
            self.assertEqual(json.loads(args.receipt.read_text())['status'], 'uploaded')
            with patch.object(pdf.subprocess, 'check_output') as run:
                with self.assertRaises(FileExistsError): pdf.publish(args)
                run.assert_not_called()

    def test_image_only_pdf_rejected(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp)
            doc=pymupdf.open()
            doc.new_page()
            doc.save(root/'empty.pdf')
            with self.assertRaisesRegex(ValueError, 'searchable text'):
                pdf.inspect(root/'empty.pdf', root/'pages')

if __name__ == '__main__': unittest.main()
