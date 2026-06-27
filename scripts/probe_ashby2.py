"""Probe Ashby location-combobox dropdown markup and hidden radio inputs."""
import json

from playwright.sync_api import sync_playwright

URL = "https://jobs.ashbyhq.com/deliveroo/ce477391-9f61-48b1-9767-23bef4fb46c4/application"

with sync_playwright() as p:
    b = p.chromium.launch(headless=True)
    page = b.new_page()
    page.goto(URL, wait_until="domcontentloaded", timeout=45000)
    page.wait_for_timeout(4000)

    loc = page.locator("input[role=combobox]").first
    loc.click()
    loc.press_sequentially("Bengaluru", delay=60)
    page.wait_for_timeout(2500)
    dropdown = page.evaluate(
        """() => {
        const out = [];
        document.querySelectorAll("[role='option'], [class*='option'], li").forEach(el => {
            const t = (el.textContent || '').trim();
            if (t && t.length < 80 && el.offsetParent !== null)
                out.push({tag: el.tagName, role: el.getAttribute('role'),
                          cls: (el.className || '').slice(0, 60), text: t.slice(0, 50)});
        });
        return out.slice(0, 10);
    }"""
    )
    radios = page.evaluate(
        """() => {
        const out = [];
        document.querySelectorAll("input[type=radio]").forEach(el => {
            out.push({name: el.name, value: (el.value || '').slice(0, 60),
                      visible: el.offsetParent !== null,
                      labelText: (el.closest('label')?.textContent ||
                                  document.querySelector(`label[for='${el.id}']`)?.textContent ||
                                  '').slice(0, 80)});
        });
        return out.slice(0, 12);
    }"""
    )
    print(json.dumps({"dropdown": dropdown, "radios": radios}, indent=1)[:4000])
    b.close()
