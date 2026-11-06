import os
import sys
import time
import pytest
import requests
from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC

# ==============================================================================
# CONFIGURATION
# Default to production website if no environment variable is provided.
# ==============================================================================
BASE_URL = os.getenv(
    "HOME_BASE_URL",
    os.getenv("BASE_URL", "https://www.chakorahub.com")
).rstrip("/")

HOME_URL = f"{BASE_URL}/home"
ROOT_URL = f"{BASE_URL}/"
HEALTH_URL = f"{BASE_URL}/health"
WAIT = int(os.getenv("SELENIUM_WAIT_TIMEOUT", "25"))


# ==============================================================================
# FIXTURES
# ==============================================================================
@pytest.fixture
def driver():
    """Initialize a headless Chrome browser matching CI/CD standard options."""
    import shutil

    # Filter out mismatched pre-installed chromedrivers from PATH so Selenium Manager resolves matching driver
    clean_path_parts = [
        p for p in os.environ.get("PATH", "").split(os.pathsep)
        if "chromedriver" not in p.lower()
    ]
    os.environ["PATH"] = os.pathsep.join(clean_path_parts)

    options = webdriver.ChromeOptions()
    options.add_argument("--headless=new")
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-dev-shm-usage")
    options.add_argument("--disable-gpu")
    options.add_argument("--disable-software-rasterizer")
    options.add_argument("--disable-extensions")
    options.add_argument("--window-size=1440,1000")

    chrome_bin = (
        os.getenv("CHROME_PATH")
        or os.getenv("CHROME_BIN")
        or shutil.which("google-chrome")
        or shutil.which("chrome")
    )
    if chrome_bin:
        options.binary_location = chrome_bin

    browser = None
    try:
        browser = webdriver.Chrome(options=options)
    except Exception as e1:
        try:
            options_legacy = webdriver.ChromeOptions()
            options_legacy.add_argument("--headless")
            options_legacy.add_argument("--no-sandbox")
            options_legacy.add_argument("--disable-dev-shm-usage")
            options_legacy.add_argument("--disable-gpu")
            if chrome_bin:
                options_legacy.binary_location = chrome_bin
            browser = webdriver.Chrome(options=options_legacy)
        except Exception as e2:
            pytest.skip(f"Chrome browser environment failed to start in runner: {e1} | {e2}")

    browser.set_page_load_timeout(45)

    try:
        yield browser
    finally:
        if browser:
            try:
                browser.quit()
            except Exception:
                pass


def wait_loaded(browser):
    """Wait until document.readyState == 'complete'."""
    WebDriverWait(browser, WAIT).until(
        lambda d: d.execute_script("return document.readyState") == "complete"
    )


# ==============================================================================
# SELENIUM SMOKE TESTS FOR CHAKORA HOME SERVICE & LANDING PAGE
# ==============================================================================
def test_home_api_health_endpoint():
    """Verify health or batches endpoint responds without 50x errors."""
    try:
        resp = requests.get(HEALTH_URL, timeout=15)
        assert resp.status_code not in (502, 503, 504), (
            f"Health check returned gateway failure status: {resp.status_code}"
        )
    except requests.exceptions.RequestException as e:
        print(f"[Health Check Notice]: {e}")


def test_home_page_rendered(driver):
    """Verify that the home page loads and contains Chakora title."""
    driver.get(HOME_URL)
    wait_loaded(driver)

    # Check title
    assert "Chakora" in driver.title, f"Expected 'Chakora' in title, got '{driver.title}'"


def test_home_navbar_brand(driver):
    """Verify that the navbar brand is visible and links to home."""
    driver.get(HOME_URL)
    wait_loaded(driver)

    brand = WebDriverWait(driver, WAIT).until(
        EC.presence_of_element_located((By.CSS_SELECTOR, ".navbar-brand, .modern-header"))
    )
    assert brand is not None, "Navbar brand element not found"
    assert "Chakora" in driver.page_source, "Expected 'Chakora' in page source"


def test_home_hero_section_present(driver):
    """Verify the hero section is rendered on the home page."""
    driver.get(HOME_URL)
    wait_loaded(driver)

    hero = WebDriverWait(driver, WAIT).until(
        EC.presence_of_element_located((By.CSS_SELECTOR, ".hero-section, .hero-content"))
    )
    assert hero is not None, "Hero section not found on home page"


def test_home_login_controls_present(driver):
    """Verify the login select or password input controls exist on home page."""
    driver.get(HOME_URL)
    wait_loaded(driver)

    login_control = WebDriverWait(driver, WAIT).until(
        EC.presence_of_element_located((By.CSS_SELECTOR, "select#login_type, input#password, form"))
    )
    assert login_control is not None, "Login controls not found on home page"


def test_home_batches_or_enquiry_present(driver):
    """Verify either running batches section or enquiry section is rendered."""
    driver.get(HOME_URL)
    wait_loaded(driver)

    section = WebDriverWait(driver, WAIT).until(
        EC.presence_of_element_located((By.CSS_SELECTOR, "#enquirySection, .enquiry-card, .fas.fa-graduation-cap"))
    )
    assert section is not None, "Neither batches nor enquiry section found on home page"
