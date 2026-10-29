"""Browser check for Streamlit read-aloud: starts a round with "Read objections aloud" ticked and confirms the page
asks the browser to speak the objection once, and again on "Read again". speechSynthesis is stubbed to record calls.

Needs the app running locally (streamlit run streamlit_app.py) and Playwright with Chromium. No Gemini calls beyond
the app's own startup ping.

Run: python tests/check_read_aloud.py [URL]   (default http://localhost:8501; exit 0 on pass)
"""
import sys

from playwright.sync_api import sync_playwright

STUB = """window.__spoken = [];
const s = window.speechSynthesis;
s.speak = (u) => window.__spoken.push(u.text);
s.cancel = () => {};"""


def main(url):
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page()
        page.add_init_script(STUB)
        page.goto(url)
        page.get_by_text("Read objections aloud").click()
        page.get_by_role("button", name="Start round").click()
        page.get_by_text("Objection 1 of 5").wait_for(timeout=30000)
        page.wait_for_timeout(1500)
        heading = page.locator("h3").first.inner_text().strip()
        spoken = page.evaluate("window.__spoken")
        first_ok = spoken == [heading]
        print(("PASS" if first_ok else "FAIL") + f" objection spoken once: {len(spoken)} call(s)")
        page.get_by_role("button", name="Read again").click()
        spoken = page.evaluate("window.__spoken")
        again_ok = len(spoken) == 2 and spoken[1] == heading
        print(("PASS" if again_ok else "FAIL") + " Read again speaks it again")
        clock = page.get_by_text("Reading aloud - the clock starts after.").count() > 0
        print(("PASS" if clock else "FAIL") + " clock waits for the reading")
        browser.close()
    return 0 if first_ok and again_ok and clock else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1] if len(sys.argv) > 1 else "http://localhost:8501"))
