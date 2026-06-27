"""Dump the DOM structure of Ashby form controls our extractor misses:
yes/no button groups, the location autocomplete, and file-upload labels."""
import json
import sys

from playwright.sync_api import sync_playwright

URL = "https://jobs.ashbyhq.com/airwallex/b5e329cd-e842-4404-bda0-3bc0f990f170/application"

with sync_playwright() as p:
    b = p.chromium.launch(headless=True)
    page = b.new_page()
    page.goto(URL, wait_until="domcontentloaded", timeout=45000)
    page.wait_for_timeout(4000)

    info = page.evaluate(
        """() => {
        const out = {file_inputs: [], yesno_groups: [], location: null};
        document.querySelectorAll('input[type=file]').forEach((el, i) => {
            const wrap = el.closest('div[class*=field], div[class*=upload], section, form') || el.parentElement;
            out.file_inputs.push({
                idx: i,
                label: (wrap ? wrap.textContent : '').slice(0, 120),
                attrs: {id: el.id, name: el.name, accept: el.accept},
            });
        });
        // Ashby yes/no: pairs of <button> with exact Yes / No text
        const seen = new Set();
        document.querySelectorAll('button').forEach(b => {
            const t = (b.textContent || '').trim();
            if (t !== 'Yes' && t !== 'No') return;
            const group = b.closest('div[class*=field], fieldset, div[class*=yesno], div[class*=container]') || b.parentElement;
            if (seen.has(group)) return;
            seen.add(group);
            const label = (group.textContent || '').slice(0, 200);
            out.yesno_groups.push({
                label,
                buttons: Array.from(group.querySelectorAll('button')).map(x => ({
                    text: (x.textContent || '').trim(),
                    cls: x.className.slice(0, 80),
                    pressed: x.getAttribute('aria-pressed'),
                    id: x.id,
                })),
                group_cls: group.className.slice(0, 100),
            });
        });
        const locEl = document.querySelector('input[placeholder*="typing" i], input[aria-label*=location i], input[id*=location i]');
        if (locEl) {
            out.location = {placeholder: locEl.placeholder, id: locEl.id,
                            name: locEl.name, role: locEl.getAttribute('role'),
                            aria: locEl.getAttribute('aria-autocomplete'),
                            cls: locEl.className.slice(0, 80)};
        }
        return out;
    }"""
    )
    print(json.dumps(info, indent=2)[:6000])
    b.close()
