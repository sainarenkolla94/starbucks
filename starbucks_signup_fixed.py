"""
FIXED v4 — Selenium automation for the Starbucks / Auth0 signup page
    starbucks.com/account/create  ->  auth.starbucks.com/u/signup

What was wrong in v3 (found by the user, confirmed in the code):
  The dry-run only printed the NAMES of the fields the classifier had
  found — e.g. filled ['email', 'first_name', ...]. It NEVER read the
  values back out of the boxes. So "email filled" was an assumption:
  the email could have gone into the wrong input, been cleared by the
  React/Auth0 controlled component on blur, or never landed at all,
  and v3 would still have reported OK and submitted anyway.

What v4 changes — verify, don't assume:
 1. SET-AND-VERIFY per field: click to focus, clear, type, then READ
    BACK el.get_attribute('value') and compare with the expected value.
    Retry 1 = re-type slowly. Retry 2 = JS native value-setter +
    'input'/'change' events (the method React controlled inputs accept).
    A field is only "filled" when its read-back matches exactly.
 2. INPUT DUMP: before filling and on any failure, prints every visible
    input with type/name/id/autocomplete/placeholder/aria-label/label
    and its current value (password masked) — so a mis-mapped email
    field is visible in the output, not a guess.
 3. VERIFY GATE before submit: re-finds all 4 fields from scratch and
    re-reads them. If any value does not match users.csv, it does NOT
    submit — it saves filled_form.png + filled_page.html next to the
    script and stops with the mismatch printed.
 4. TERMS: v3 skipped checkboxes that were not displayed; Auth0's terms
    checkbox can be visually hidden. v4 checks it by JS click regardless
    of visibility, and logs whether it ended up selected.
 5. POST-SUBMIT VERIFICATION: v3 slept 3s and printed OK no matter what.
    v4 polls after submit: SUCCESS only when the signup form is gone /
    the browser has left the signup URL; page error/validation texts
    are collected and reported as FAILED; anything else is UNKNOWN
    with after_submit.png + after_submit.html saved. No fake OKs.

Kept from v3 (those fixes were correct and proven by the Mac dry-run):
 one shared polling deadline (no stacked timeouts), headless OFF by
 default, macOS/Windows/Linux Chrome paths, cookie-banner dismissal,
 human-verification wait, fresh temp profile per user.

Usage (from the folder containing this file and users.csv):
  pip install selenium
  python starbucks_signup_fixed.py --dry-run   # fill + VERIFY first user, no submit
  python starbucks_signup_fixed.py --limit 1   # first user, submit + verify outcome
  python starbucks_signup_fixed.py             # all users
"""

import argparse, csv, shutil, sys, tempfile, time
from pathlib import Path

from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys

SIGNUP_URL = "https://www.starbucks.com/account/create"
CSV_FILE = Path(__file__).with_name("users.csv")
HERE = Path(__file__).parent
SELECT_ALL = Keys.COMMAND if sys.platform == "darwin" else Keys.CONTROL

CHROME_CANDIDATES = [
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    "/Applications/Chromium.app/Contents/MacOS/Chromium",
    "/opt/meta-chromium/chrome",
    "/usr/bin/google-chrome", "/usr/bin/google-chrome-stable",
    "/usr/bin/chromium", "/usr/bin/chromium-browser",
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
]

# ---------------------------------------------------------------- driver

def find_chrome():
    for p in CHROME_CANDIDATES:
        if Path(p).exists():
            return p
    for name in ("google-chrome", "chromium", "chromium-browser", "chrome"):
        found = shutil.which(name)
        if found:
            return found
    return None

def fresh_driver(headless=False):
    profile = tempfile.mkdtemp(prefix="chrome_profile_")
    opts = Options()
    binary = find_chrome()
    if binary:
        opts.binary_location = binary
    opts.add_argument(f"--user-data-dir={profile}")
    opts.add_argument("--no-first-run")
    opts.add_argument("--no-default-browser-check")
    opts.add_argument("--disable-extensions")
    opts.add_argument("--window-size=1280,900")
    if headless:
        opts.add_argument("--headless=new")
    if sys.platform.startswith("linux"):
        opts.add_argument("--no-sandbox")
        opts.add_argument("--disable-dev-shm-usage")
        opts.add_argument("--disable-gpu")
    driver = webdriver.Chrome(options=opts)
    driver.set_page_load_timeout(60)
    return driver, profile

# ---------------------------------------------------------------- inspection

LABEL_JS = """
const el = arguments[0];
let text = '';
if (el.id) {
  const lab = document.querySelector(`label[for="${CSS.escape(el.id)}"]`);
  if (lab) text = lab.innerText || lab.textContent || '';
}
if (!text) {
  const wrap = el.closest('label');
  if (wrap) text = wrap.innerText || wrap.textContent || '';
}
if (!text && el.getAttribute('aria-labelledby')) {
  text = el.getAttribute('aria-labelledby').split(/\\s+/)
    .map(id => document.getElementById(id)?.textContent || '').join(' ');
}
return text.trim();
"""

def describe_inputs(driver, visible_only=True):
    out = []
    for el in driver.find_elements(By.TAG_NAME, "input"):
        try:
            if visible_only and not el.is_displayed():
                continue
            t = (el.get_attribute("type") or "text").lower()
            if t in ("hidden", "submit", "button", "radio", "file"):
                continue
            info = {"el": el, "type": t,
                    "name": (el.get_attribute("name") or "").lower(),
                    "id": (el.get_attribute("id") or "").lower(),
                    "autocomplete": (el.get_attribute("autocomplete") or "").lower(),
                    "placeholder": (el.get_attribute("placeholder") or "").lower(),
                    "aria": (el.get_attribute("aria-label") or "").lower()}
            try:
                info["label"] = driver.execute_script(LABEL_JS, el).lower()
            except Exception:
                info["label"] = ""
            info["haystack"] = " ".join(info[k] for k in
                                        ("name", "id", "autocomplete", "placeholder", "aria", "label"))
            out.append(info)
        except Exception:
            continue
    return out

def classify(inputs):
    fields, used = {}, set()
    def take(pred):
        for i, info in enumerate(inputs):
            if i not in used and pred(info):
                used.add(i)
                return info["el"]
        return None
    # Email is matched STRICTLY first by type/autocomplete/name — a loose
    # 'email in label text' match is only the second pass, so a wrapping
    # label containing the whole form's text cannot hijack the first box.
    fields["email"] = take(lambda i: i["type"] == "email" or i["autocomplete"] == "email"
                            or i["name"] in ("email", "emailaddress", "email_address", "username"))
    if not fields["email"]:
        fields["email"] = take(lambda i: "email" in i["name"] or "email" in i["id"])
    if not fields["email"]:
        fields["email"] = take(lambda i: i["label"].strip() == "email address"
                                or i["aria"].strip() == "email address")
    fields["password"] = take(lambda i: i["type"] == "password"
                              or i["autocomplete"] in ("new-password", "current-password"))
    fields["first_name"] = take(lambda i: "given-name" in i["autocomplete"] or "first" in i["haystack"])
    fields["last_name"] = take(lambda i: "family-name" in i["autocomplete"]
                               or "last" in i["haystack"] or "family" in i["haystack"])
    if not fields["email"]:
        fields["email"] = take(lambda i: "email" in i["haystack"])
    rest = [info["el"] for i, info in enumerate(inputs)
            if i not in used and info["type"] in ("text", "")]
    for key in ("first_name", "last_name"):
        if not fields.get(key) and rest:
            fields[key] = rest.pop(0)
    return {k: v for k, v in fields.items() if v is not None}

def dump_inputs(driver, stage):
    print(f"  [INPUTS @ {stage}]")
    for n, info in enumerate(describe_inputs(driver, visible_only=False)):
        try:
            val = info["el"].get_attribute("value") or ""
        except Exception:
            val = "<unreadable>"
        shown = f"*** (len {len(val)})" if info["type"] == "password" else repr(val)
        disp = ""
        try:
            disp = " visible" if info["el"].is_displayed() else " HIDDEN"
        except Exception:
            pass
        print(f"    [{n}] type={info['type']} name={info['name']!r} id={info['id']!r} "
              f"autocomplete={info['autocomplete']!r} aria={info['aria']!r} "
              f"label={info['label']!r}{disp} value={shown}")

def page_text(driver):
    try:
        return driver.find_element(By.TAG_NAME, "body").text
    except Exception:
        return ""

def save_artifacts(driver, prefix):
    try:
        driver.save_screenshot(str(HERE / f"{prefix}.png"))
    except Exception:
        pass
    try:
        (HERE / f"{prefix}.html").write_text(driver.page_source, encoding="utf-8")
    except Exception:
        pass
    print(f"  [saved {prefix}.png + {prefix}.html in {HERE}]")

# ---------------------------------------------------------------- form wait

def dismiss_cookie_banner(driver):
    for loc in [(By.ID, "onetrust-accept-btn-handler"),
                (By.XPATH, "//button[contains(translate(.,'ABCDEFGHIJKLMNOPQRSTUVWXYZ','abcdefghijklmnopqrstuvwxyz'),'accept all')]"),
                (By.XPATH, "//button[normalize-space()='Accept']")]:
        try:
            for el in driver.find_elements(*loc):
                if el.is_displayed():
                    driver.execute_script("arguments[0].click();", el)
                    return True
        except Exception:
            pass
    return False

def wait_for_form(driver, timeout=45, headless=False):
    deadline = time.time() + timeout
    banner_done = False
    warned = False
    while time.time() < deadline:
        if not banner_done:
            banner_done = dismiss_cookie_banner(driver)
        fields = classify(describe_inputs(driver))
        if all(k in fields for k in ("first_name", "last_name", "email", "password")):
            return fields
        text = page_text(driver).lower()
        if any(w in text for w in ("captcha", "verify you are human", "are you a robot",
                                   "unusual traffic", "checking your browser")):
            if headless:
                save_artifacts(driver, "timeout_diagnostics")
                raise RuntimeError("Human-verification check in headless Chrome — re-run without --headless.")
            if not warned:
                warned = True
                print("  [!] Human-verification check on screen — solve it in the window; I'll keep waiting...")
                deadline = max(deadline, time.time() + 120)
        time.sleep(0.5)
    dump_inputs(driver, "timeout")
    save_artifacts(driver, "timeout_diagnostics")
    raise RuntimeError(f"Signup form did not appear within {timeout}s. URL={driver.current_url} title={driver.title!r}")

# ---------------------------------------------------------------- verified fill

JS_SET_VALUE = """
const el = arguments[0], val = arguments[1];
const proto = Object.getPrototypeOf(el);
const desc = Object.getOwnPropertyDescriptor(proto, 'value')
          || Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, 'value');
if (desc && desc.set) desc.set.call(el, val); else el.value = val;
el.dispatchEvent(new Event('input', {bubbles: true}));
el.dispatchEvent(new Event('change', {bubbles: true}));
el.dispatchEvent(new Event('blur', {bubbles: true}));
return el.value;
"""

def read_value(el):
    try:
        return el.get_attribute("value") or ""
    except Exception:
        return ""

def set_and_verify(driver, el, field, expected):
    """Fill one field and READ BACK. True only if the box holds `expected`."""
    shown = "***" if field == "password" else repr(expected)
    for attempt in (1, 2, 3):
        try:
            driver.execute_script("arguments[0].scrollIntoView({block:'center'});", el)
            try:
                el.click()
            except Exception:
                driver.execute_script("arguments[0].focus();", el)
            if attempt == 1:
                el.clear()
                el.send_keys(expected)
            elif attempt == 2:
                el.send_keys(SELECT_ALL)
                el.send_keys(Keys.DELETE)
                for ch in expected:  # slow typing — some React forms drop fast input
                    el.send_keys(ch)
                    time.sleep(0.03)
            else:
                driver.execute_script(JS_SET_VALUE, el, expected)
            time.sleep(0.3)
            actual = read_value(el)
            if actual == expected:
                print(f"  [FILLED+VERIFIED] {field} = {shown} (attempt {attempt})")
                return True
            got = "***" if field == "password" else repr(actual)
            print(f"  [fill attempt {attempt}] {field}: expected {shown}, box actually holds {got}")
        except Exception as e:
            print(f"  [fill attempt {attempt}] {field}: {type(e).__name__}: {str(e)[:200]}")
    return False

def verify_gate(driver, user):
    """Re-find every field from scratch and re-read it. Submit is blocked
    unless all four boxes hold exactly the users.csv values."""
    print("  [VERIFY GATE] re-checking all fields before submit...")
    fields = classify(describe_inputs(driver))
    ok = True
    for key in ("first_name", "last_name", "email", "password"):
        el = fields.get(key)
        if el is None:
            print(f"  [VERIFY FAIL] {key}: field not found on re-check")
            ok = False
            continue
        actual = read_value(el)
        match = actual == user[key]
        got = "***" if key == "password" else repr(actual)
        want = "***" if key == "password" else repr(user[key])
        print(f"  [VERIFY {'PASS' if match else 'FAIL'}] {key}: box holds {got}, expected {want}")
        ok = ok and match
    # Where did the email actually end up? (catches a mis-mapped field)
    holders = []
    for n, info in enumerate(describe_inputs(driver, visible_only=False)):
        if read_value(info["el"]) == user["email"]:
            holders.append(f"input[{n}] name={info['name']!r} id={info['id']!r} label={info['label']!r}")
    print(f"  [VERIFY] email value found in: {holders or 'NO input — email did not land anywhere'}")
    return ok

# ---------------------------------------------------------------- terms + submit

def click_terms(driver):
    cbs = driver.find_elements(By.CSS_SELECTOR, "input[type='checkbox']")
    for el in cbs:
        try:
            label = ""
            try:
                label = driver.execute_script(LABEL_JS, el).lower()
            except Exception:
                pass
            if not label:
                try:
                    label = (driver.execute_script(
                        "return (arguments[0].closest('label')?.textContent || "
                        "arguments[0].parentElement?.textContent || '').trim();", el) or "").lower()
                except Exception:
                    label = ""
            if "term" in label or "accept" in label:
                if el.is_selected():
                    print("  [terms] already selected")
                    return True
                driver.execute_script("arguments[0].click();", el)
                time.sleep(0.3)
                sel = el.is_selected()
                print(f"  [terms] clicked 'Terms of Use' checkbox, selected={sel}")
                return sel
        except Exception:
            pass
    print("  [terms] Terms checkbox not found")
    return False

def click_submit(driver, timeout=15):
    deadline = time.time() + timeout
    while time.time() < deadline:
        buttons = []
        for el in driver.find_elements(By.TAG_NAME, "button"):
            try:
                if el.is_displayed() and el.is_enabled():
                    buttons.append(el)
            except Exception:
                pass
        for want_exact in (True, False):
            for el in buttons:
                try:
                    text = (el.text or "").strip().lower()
                    if want_exact and text in ("create account", "sign up", "create my account"):
                        driver.execute_script("arguments[0].scrollIntoView({block:'center'});", el)
                        driver.execute_script("arguments[0].click();", el)
                        print(f"  [submit] clicked button {text!r}")
                        return True
                    if not want_exact and el.get_attribute("type") == "submit":
                        driver.execute_script("arguments[0].click();", el)
                        print("  [submit] clicked a type=submit button")
                        return True
                except Exception:
                    pass
        time.sleep(0.5)
    return False

ERROR_JS = """
const out = [];
document.querySelectorAll('[role="alert"], [aria-invalid="true"], '
  + '[class*="error" i], [id*="error" i]').forEach(n => {
  const t = (n.innerText || n.textContent || '').trim();
  if (t) out.push(t.slice(0, 200));
});
return [...new Set(out)].slice(0, 8);
"""

def post_submit_result(driver, timeout=45, headless=False):
    deadline = time.time() + timeout
    warned = False
    error_since = None
    while time.time() < deadline:
        url = driver.current_url
        text = page_text(driver)
        low = text.lower()
        if any(w in low for w in ("captcha", "verify you are human", "are you a robot")):
            if headless:
                return "FAILED", ["Human-verification check after submit (headless)"]
            if not warned:
                warned = True
                print("  [!] Verification check after submit — solve it in the window; I'll keep waiting...")
                deadline = max(deadline, time.time() + 120)
            time.sleep(1)
            continue
        try:
            errors = driver.execute_script(ERROR_JS)
        except Exception:
            errors = []
        still_has_password = any(i["type"] == "password" for i in describe_inputs(driver))
        left_signup = ("/u/signup" not in url) and ("/account/create" not in url)
        if left_signup or not still_has_password:
            return "SUCCESS", [f"signup form completed/gone, now at -> {url}"]
        if errors:
            if error_since is None:
                error_since = time.time()
            elif time.time() - error_since > 5:
                return "FAILED", errors
        time.sleep(1)
    errs = []
    try:
        errs = driver.execute_script(ERROR_JS)
    except Exception:
        pass
    return "UNKNOWN", errs or [f"still on signup page after {timeout}s: {driver.current_url}",
                               f"visible text: {page_text(driver)[:300]!r}"]

# ---------------------------------------------------------------- flow

def fill_signup(driver, user, dry_run=False, wait=45, headless=False):
    driver.get(SIGNUP_URL)
    fields = wait_for_form(driver, timeout=wait, headless=headless)
    dump_inputs(driver, "before fill")
    all_set = True
    for key in ("first_name", "last_name", "email", "password"):
        el = fields.get(key)
        if el is None:
            print(f"  [MISSING] no element classified as {key}")
            all_set = False
            continue
        if not set_and_verify(driver, el, key, user[key]):
            all_set = False
    save_artifacts(driver, "filled_form")
    if not (all_set and verify_gate(driver, user)):
        raise RuntimeError("Verification gate FAILED — the form does NOT hold the "
                           "users.csv values (see dump above + filled_form.png). "
                           "NOT submitting a broken form.")
    if dry_run:
        print(f"  [DRY-RUN OK] all 4 values verified in the correct boxes for {user['email']}, NOT submitted")
        return "DRY_RUN_OK"
    if not click_terms(driver):
        print("  [warn] could not confirm Terms checkbox — submitting anyway, post-submit check will judge")
    if not click_submit(driver):
        save_artifacts(driver, "after_submit")
        return "FAILED: submit button not found"
    status, details = post_submit_result(driver, headless=headless)
    save_artifacts(driver, "after_submit")
    print(f"  [RESULT {status}] {user['email']}: {details}")
    return status

def load_users(limit=None):
    if not CSV_FILE.exists():
        sys.exit(f"users.csv not found at {CSV_FILE}")
    with CSV_FILE.open(newline="", encoding="utf-8") as f:
        users = list(csv.DictReader(f))
    bad = [i + 1 for i, u in enumerate(users)
           if not all(u.get(k) for k in ("first_name", "last_name", "email", "password"))]
    if bad:
        sys.exit(f"users.csv rows missing fields: {bad}")
    return users[:limit] if limit else users

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--limit", type=int, default=None)
    ap.add_argument("--headless", action="store_true",
                    help="opt in to headless (OFF by default)")
    ap.add_argument("--wait", type=int, default=45)
    ap.add_argument("--url", default=None)
    args = ap.parse_args()
    global SIGNUP_URL
    if args.url:
        SIGNUP_URL = args.url
    users = load_users(args.limit or (1 if args.dry_run else None))
    print(f"Chrome binary: {find_chrome()} | Headless: {args.headless} | "
          f"URL: {SIGNUP_URL} | Total: {len(users)} | Dry-run: {args.dry_run}")
    ok = failed = 0
    for i, user in enumerate(users, 1):
        print(f"\n--- {i}/{len(users)} {user['email']} ---")
        driver = profile = None
        try:
            driver, profile = fresh_driver(headless=args.headless)
            result = fill_signup(driver, user, dry_run=args.dry_run,
                                 wait=args.wait, headless=args.headless)
            if result in ("SUCCESS", "DRY_RUN_OK"):
                ok += 1
            else:
                failed += 1
        except Exception as e:
            failed += 1
            print(f"  [FAILED] {user['email']}: {type(e).__name__}: {e}")
        finally:
            if driver:
                try: driver.quit()
                except Exception: pass
            if profile:
                shutil.rmtree(profile, ignore_errors=True)
    print(f"\nSummary: OK={ok} FAILED={failed} TOTAL={len(users)}")
    return 0 if failed == 0 else 1

if __name__ == "__main__":
    sys.exit(main())
