#!/usr/bin/env python3
"""Quick regression tests for oddmuse_to_markdown.py; stdlib only."""
import base64
import gzip
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent


def record(path, **fields):
    path.parent.mkdir(parents=True, exist_ok=True)
    encoded = ''.join(f'{key}: {str(value).replace(chr(10), chr(10)+chr(9))}\n' for key, value in fields.items())
    path.write_text(encoded, encoding='utf-8')


class ExportTests(unittest.TestCase):
    def test_flat_and_nested_and_media(self):
        with tempfile.TemporaryDirectory() as t:
            root = Path(t) / 'dump'
            out = Path(t) / 'site'
            record(root / 'page' / 'HomePage.pg', revision='2', ts='1700000000',
                   text="== Intro ==\nLink to [[Another Page|another]] and TestPage.\n" \
                        "Also [https://example.org Example] and '''bold''' with ''italics''.\n" \
                        "* First\n** Second\n# One\n----\n[[image:Picture]]\n")
            record(root / 'page' / 'A' / 'Another_Page.pg', revision='1', ts='1690000000',
                   text='A wiki page.\n|| Name || Value ||\n|| A || B ||\n')
            record(root / 'page' / 'T' / 'TestPage.pg', revision='1', text='CamelCase target\n')
            record(root / 'keep' / 'H' / 'HomePage' / '1.kp', revision='1', ts='1600000000',
                   text='== Old ==\nA historic [[Another Page]] link.\n')
            record(root / 'page' / 'Picture.pg', revision='1',
                   text='#FILE image/png gzip\n' + base64.b64encode(gzip.compress(b'\x89PNG test')).decode())
            proc = subprocess.run([sys.executable, str(HERE/'oddmuse_to_markdown.py'), str(root), str(out), '--history'], capture_output=True, text=True)
            self.assertEqual(proc.returncode, 0, proc.stderr)
            homepage = (out / 'pages' / 'HomePage.md').read_text()
            self.assertIn('## Intro', homepage)
            self.assertIn('[another](Another_Page.md)', homepage)
            self.assertIn('[TestPage](TestPage.md)', homepage)
            self.assertIn('[Example](https://example.org)', homepage)
            self.assertIn('**bold**', homepage)
            self.assertIn('*italics*', homepage)
            self.assertIn('![Picture](../assets/Picture.png)', homepage)
            self.assertEqual((out / 'assets' / 'Picture.png').read_bytes(), b'\x89PNG test')
            self.assertIn('## Old', (out / 'history' / 'HomePage' / '1.md').read_text())
            self.assertEqual(json.loads((out / 'manifest.json').read_text())['totals']['pages'], 4)

    def test_markdown_raw_and_unicode(self):
        with tempfile.TemporaryDirectory() as t:
            root = Path(t) / 'page'
            out = Path(t) / 'out'
            record(root / 'Résumé.pg', revision='1', text='#MARKDOWN\n### Already Markdown\n**Strong**\n')
            proc = subprocess.run([sys.executable, str(HERE/'oddmuse_to_markdown.py'), str(root), str(out)], capture_output=True, text=True)
            self.assertEqual(proc.returncode, 0, proc.stderr)
            content = (out/'pages'/'Résumé.md').read_text()
            self.assertIn('### Already Markdown', content)
            self.assertIn('**Strong**', content)


if __name__ == '__main__':
    unittest.main()
