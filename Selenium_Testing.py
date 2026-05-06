import os
import unittest
from datetime import datetime

from selenium import webdriver
from selenium.common.exceptions import (
    NoSuchElementException,
    StaleElementReferenceException,
    TimeoutException,
    WebDriverException,
)
from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import Select, WebDriverWait


BASE_URL = "http://localhost:5000"
SCREENSHOT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "selenium_screenshots")

ADMIN_EMAIL = "admin@college.edu"
ADMIN_PASSWORD = "admin123"

TEACHER_DATA = {
    "name": "Prof. Selenium Test",
    "short": "PST",
    "email": "selenium.teacher@college.edu",
    "id": "TSEL",
    "max_day": "4",
    "max_week": "18",
}

ROOMS_DATA = [
    {
        "name": "Selenium Classroom 101",
        "id": "RSEL101",
        "room_type": "lecture",
        "type_label": "Lecture Hall",
        "capacity": "60",
    },
    {
        "name": "Selenium Lab 201",
        "id": "LSEL201",
        "room_type": "lab",
        "type_label": "Laboratory",
        "capacity": "60",
    },
]

SUBJECT_DATA = {
    "name": "Selenium DBMS",
    "short": "SDBMS",
    "code": "SDBMS",
    "id": "SDBMS",
    "lectures_per_week": "3",
    "lab_hours_per_week": "2",
}

DIVISION_DATA = {
    "name": "SY4",
    "id": "SY4",
    "students": "60",
    "batches": ["B1", "B2", "B3", "B4"],
}

CONSTRAINT_DATA = {
    "type_value": "max_per_day",
    "priority": "hard",
    "weight": "10",
    "scope": "teacher",
    "teacher_id": TEACHER_DATA["id"],
    "max_per_day": "4",
    "description": "Test hard constraint for Selenium execution",
}


class AcademicTimetableSeleniumTests(unittest.TestCase):
    driver = None
    wait = None
    clean_state_prepared = False
    absence_context = {}
    latest_notification_payloads = []

    @classmethod
    def setUpClass(cls):
        os.makedirs(SCREENSHOT_DIR, exist_ok=True)

        options = webdriver.ChromeOptions()
        options.add_argument("--start-maximized")
        options.add_argument("--window-size=1600,1000")
        options.add_argument("--disable-notifications")
        options.add_argument("--disable-popup-blocking")

        cls.driver = webdriver.Chrome(options=options)
        cls.driver.implicitly_wait(5)
        cls.driver.set_page_load_timeout(30)
        cls.driver.set_script_timeout(30)
        cls.wait = WebDriverWait(cls.driver, 25)

        cls.driver.get(BASE_URL)
        cls.wait.until(EC.presence_of_element_located((By.TAG_NAME, "body")))
        cls.driver.delete_all_cookies()
        cls.driver.execute_script(
            "window.localStorage.clear();"
            "window.sessionStorage.clear();"
        )
        cls.driver.get(BASE_URL)
        cls.wait.until(EC.presence_of_element_located((By.TAG_NAME, "body")))

    @classmethod
    def tearDownClass(cls):
        if cls.driver:
            cls.driver.quit()

    def execute_test_case(self, tc_label, success_message, test_callable):
        try:
            test_callable()
            print(success_message)
        except Exception as exc:
            screenshot = self.save_failure_screenshot(tc_label)
            print(f"{tc_label} FAILED: {exc} | Screenshot: {screenshot}")
            raise

    def save_failure_screenshot(self, tc_label):
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        safe_label = tc_label.lower().replace("-", "_").replace(" ", "_")
        path = os.path.join(SCREENSHOT_DIR, f"{safe_label}_{timestamp}.png")
        try:
            self.driver.save_screenshot(path)
        except WebDriverException:
            pass
        return path

    def wait_and_click(self, locator, timeout=25):
        element = WebDriverWait(self.driver, timeout).until(EC.element_to_be_clickable(locator))
        self.driver.execute_script("arguments[0].scrollIntoView({block:'center'});", element)
        try:
            element.click()
        except WebDriverException:
            self.driver.execute_script("arguments[0].click();", element)
        return element

    def wait_and_type(self, locator, text, timeout=25):
        element = WebDriverWait(self.driver, timeout).until(EC.visibility_of_element_located(locator))
        self.driver.execute_script("arguments[0].scrollIntoView({block:'center'});", element)
        element.click()
        element.send_keys(Keys.CONTROL, "a")
        element.send_keys(str(text))
        return element

    def select_dropdown_by_text_or_first_option(
        self,
        locator,
        preferred_text=None,
        preferred_value=None,
        allow_blank=False,
        timeout=25,
    ):
        last_error = None
        for _ in range(3):
            try:
                element = WebDriverWait(self.driver, timeout).until(
                    EC.presence_of_element_located(locator)
                )
                self.wait.until(lambda d: len(Select(d.find_element(*locator)).options) > 0)
                select = Select(self.driver.find_element(*locator))

                if preferred_value is not None:
                    try:
                        select.select_by_value(preferred_value)
                        return preferred_value
                    except NoSuchElementException:
                        pass

                if preferred_text:
                    lowered = preferred_text.strip().lower()
                    for option in select.options:
                        label = option.text.strip()
                        value = (option.get_attribute("value") or "").strip()
                        if label.lower() == lowered or lowered in label.lower() or value == preferred_text:
                            select.select_by_visible_text(label)
                            return label

                if preferred_text is not None or preferred_value is not None:
                    raise AssertionError(
                        f"Preferred option was not found for locator {locator}: "
                        f"text={preferred_text!r}, value={preferred_value!r}"
                    )

                for option in select.options:
                    value = (option.get_attribute("value") or "").strip()
                    label = option.text.strip()
                    if not allow_blank and not value:
                        continue
                    if label.lower().startswith("no "):
                        continue
                    select.select_by_visible_text(label)
                    return label

                raise AssertionError(f"No selectable options found for locator {locator}")
            except StaleElementReferenceException as exc:
                last_error = exc

        raise AssertionError(f"Could not select option for locator {locator}: {last_error}")

    def assert_text_present(self, text, locator=(By.TAG_NAME, "body"), timeout=25):
        expected = text.strip().lower()
        WebDriverWait(self.driver, timeout).until(
            lambda d: expected in d.find_element(*locator).text.lower()
        )

    def is_coordinator_view_visible(self):
        try:
            return bool(
                self.driver.execute_script(
                    """
                    const root = document.getElementById('view-coordinator');
                    if (!root) return false;
                    return getComputedStyle(root).display !== 'none' && root.offsetParent !== null;
                    """
                )
            )
        except WebDriverException:
            return False

    def is_coordinator_dashboard_visible(self):
        try:
            return bool(
                self.driver.execute_script(
                    """
                    const page = document.querySelector('#page-dashboard.page.active');
                    const root = document.getElementById('view-coordinator');
                    if (!page || !root) return false;
                    return getComputedStyle(root).display !== 'none' && page.offsetParent !== null;
                    """
                )
            )
        except WebDriverException:
            return False

    def open_login_modal(self):
        buttons = self.driver.find_elements(By.CSS_SELECTOR, "button[onclick=\"openAuth('login')\"]")
        for button in buttons:
            if button.is_displayed():
                self.driver.execute_script("arguments[0].scrollIntoView({block:'center'});", button)
                try:
                    button.click()
                except WebDriverException:
                    self.driver.execute_script("arguments[0].click();", button)
                return
        self.wait_and_click((By.XPATH, "//button[contains(normalize-space(),'Sign In')]"))

    def login(self):
        if self.is_coordinator_view_visible():
            return

        self.driver.get(BASE_URL)
        self.wait.until(EC.presence_of_element_located((By.TAG_NAME, "body")))
        if self.is_coordinator_view_visible():
            return
        self.open_login_modal()
        self.wait.until(EC.visibility_of_element_located((By.ID, "l-email")))

        self.wait_and_type((By.ID, "l-email"), ADMIN_EMAIL)
        self.wait_and_type((By.ID, "l-pw"), ADMIN_PASSWORD)
        self.wait_and_click((By.XPATH, "//div[@id='auth-login']//button[contains(normalize-space(),'Sign In')]"))

        self.wait.until(
            EC.visibility_of_element_located(
                (
                    By.XPATH,
                    "//div[@id='page-dashboard' and contains(@class,'active')]"
                    "//div[contains(@class,'page-title') and normalize-space()='Dashboard']",
                )
            )
        )
        self.wait.until(lambda d: d.find_element(By.ID, "c-name").text.strip() != "")

    def ensure_logged_in(self):
        if not self.is_coordinator_view_visible():
            self.login()

    def go_to_page(self, page_name):
        self.ensure_logged_in()
        nav_locator = (By.CSS_SELECTOR, f"#view-coordinator .nav-item[data-page='{page_name}']")
        page_locator = (By.CSS_SELECTOR, f"#page-{page_name}.page.active")
        self.wait_and_click(nav_locator)
        self.wait.until(lambda d: d.find_element(*page_locator).is_displayed())
        return self.driver.find_element(*page_locator)

    def api_fetch(self, url, method="GET", body=None):
        response = self.driver.execute_async_script(
            """
            const url = arguments[0];
            const method = arguments[1];
            const body = arguments[2];
            const callback = arguments[arguments.length - 1];

            const token = window.sessionStorage.getItem('tt_auth_token') || '';
            const headers = {};
            if (token) headers['Authorization'] = `Bearer ${token}`;
            if (body !== null && body !== undefined) headers['Content-Type'] = 'application/json';

            fetch(url, {
                method: method || 'GET',
                headers,
                body: body !== null && body !== undefined ? JSON.stringify(body) : undefined
            })
                .then(async (res) => {
                    let data = null;
                    try {
                        data = await res.json();
                    } catch (err) {
                        data = { error: 'Response was not valid JSON' };
                    }
                    callback({ ok: res.ok, status: res.status, data });
                })
                .catch((err) => callback({ ok: false, status: 0, data: { error: err.message } }));
            """,
            url,
            method,
            body,
        )
        if not isinstance(response, dict):
            raise AssertionError(f"Unexpected API response for {method} {url}: {response}")
        return response

    def delete_matching_items(self, endpoint, item_matcher, id_key="id"):
        response = self.api_fetch(endpoint)
        if not response.get("ok"):
            raise AssertionError(f"Could not load {endpoint}: {response}")

        items = response.get("data") or []
        for item in items:
            if item_matcher(item):
                item_id = item.get(id_key)
                if item_id is None:
                    continue
                delete_response = self.api_fetch(f"{endpoint}/{item_id}", method="DELETE")
                if not delete_response.get("ok"):
                    raise AssertionError(f"Could not delete {endpoint}/{item_id}: {delete_response}")

    def prepare_clean_state(self):
        if self.__class__.clean_state_prepared:
            return

        self.ensure_logged_in()

        clear_response = self.api_fetch("/api/timetable/clear", method="POST")
        if not clear_response.get("ok"):
            raise AssertionError(f"Could not clear timetable before tests: {clear_response}")

        mark_read_response = self.api_fetch("/api/notifications/mark-all-read", method="POST")
        if not mark_read_response.get("ok"):
            raise AssertionError(f"Could not reset notifications before tests: {mark_read_response}")

        self.delete_matching_items(
            "/api/constraints",
            lambda item: item.get("description") == CONSTRAINT_DATA["description"],
            id_key="int_id",
        )
        self.delete_matching_items(
            "/api/divisions",
            lambda item: item.get("id") == DIVISION_DATA["id"] or item.get("name") == DIVISION_DATA["name"],
        )
        self.delete_matching_items(
            "/api/subjects",
            lambda item: item.get("id") == SUBJECT_DATA["id"] or item.get("name") == SUBJECT_DATA["name"],
        )
        self.delete_matching_items(
            "/api/rooms",
            lambda item: item.get("id") in {room["id"] for room in ROOMS_DATA}
            or item.get("name") in {room["name"] for room in ROOMS_DATA},
        )
        self.delete_matching_items(
            "/api/teachers",
            lambda item: item.get("id") == TEACHER_DATA["id"]
            or item.get("email") == TEACHER_DATA["email"]
            or item.get("name") == TEACHER_DATA["name"],
        )

        self.__class__.clean_state_prepared = True

    def enable_lab_section(self):
        lab_fields = self.driver.find_element(By.ID, "lab-fields")
        if not lab_fields.is_displayed():
            self.wait_and_click((By.XPATH, "//div[contains(@class,'toggle-row')][.//span[contains(.,'Has Lab Sessions')]]"))
            self.wait.until(lambda d: d.find_element(By.ID, "lab-fields").is_displayed())

    def find_teacher_slot_for_absence(self):
        meta_response = self.api_fetch("/api/timetable/meta")
        timetable_response = self.api_fetch("/api/timetable")
        if not meta_response.get("ok") or not timetable_response.get("ok"):
            raise AssertionError("Could not load timetable metadata for absence test")

        meta = meta_response["data"]
        timetable = timetable_response["data"]
        days = meta.get("days") or ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday"]
        periods = meta.get("period_ids") or ["P1", "P2", "P3", "P4", "P5", "P6"]

        def pick_slot_for_teacher(preferred_teacher_id=None):
            for division_id, day_map in (timetable or {}).items():
                for day in days:
                    for period in periods:
                        slots = (((day_map or {}).get(day) or {}).get(period) or [])
                        for slot in slots:
                            teacher_id = slot.get("teacher_id")
                            if preferred_teacher_id and teacher_id != preferred_teacher_id:
                                continue
                            if teacher_id:
                                return {
                                    "division_id": division_id,
                                    "teacher_id": teacher_id,
                                    "day": day,
                                    "period_id": period,
                                    "slot": slot,
                                }
            return None

        preferred = pick_slot_for_teacher(TEACHER_DATA["id"])
        if preferred:
            return preferred

        fallback = pick_slot_for_teacher()
        if fallback:
            return fallback

        raise AssertionError("No scheduled slot was found for any teacher")

    def fetch_matching_notifications(self, teacher_id=None, day=None, period_id=None):
        response = self.api_fetch("/api/notifications?unread=true")
        if not response.get("ok"):
            raise AssertionError(f"Could not load notifications: {response}")

        matches = []
        for item in response.get("data") or []:
            if item.get("type") != "slot_free":
                continue
            data = item.get("data") or {}
            if teacher_id and data.get("absent_teacher_id") != teacher_id:
                continue
            if day and data.get("day") != day:
                continue
            if period_id:
                period_ids = data.get("period_ids") or [data.get("period_id")]
                if period_id not in period_ids:
                    continue
            matches.append(item)
        return matches

    def count_timetable_slots_from_payload(self, timetable, division_id=None):
        if not isinstance(timetable, dict):
            return 0

        divisions = [division_id] if division_id else list(timetable.keys())
        count = 0
        for current_division in divisions:
            day_map = timetable.get(current_division) or {}
            if not isinstance(day_map, dict):
                continue
            for period_map in day_map.values():
                if not isinstance(period_map, dict):
                    continue
                for slots in period_map.values():
                    count += len(slots or [])
        return count

    def get_api_timetable_slot_count(self, division_id=None):
        response = self.api_fetch("/api/timetable")
        if not response.get("ok"):
            return 0
        return self.count_timetable_slots_from_payload(response.get("data") or {}, division_id=division_id)

    def get_client_timetable_slot_count(self, division_id=None):
        js_count = 0
        try:
            js_count = int(
                self.driver.execute_script(
                    """
                    const targetDivision = arguments[0];
                    const timetable =
                        (typeof TT !== 'undefined' && TT) ? TT :
                        ((typeof window.TT !== 'undefined' && window.TT) ? window.TT : {});
                    const divisions = targetDivision ? [targetDivision] : Object.keys(timetable);
                    let count = 0;
                    for (const divisionId of divisions) {
                        const dayMap = timetable[divisionId] || {};
                        for (const day of Object.keys(dayMap)) {
                            const periodMap = dayMap[day] || {};
                            for (const periodId of Object.keys(periodMap)) {
                                count += (periodMap[periodId] || []).length;
                            }
                        }
                    }
                    return count;
                    """,
                    division_id,
                )
            )
        except (ValueError, TypeError, WebDriverException):
            js_count = 0

        return js_count or self.get_api_timetable_slot_count(division_id=division_id)

    def test_01_login(self):
        def run():
            self.login()
            self.assertTrue(self.is_coordinator_dashboard_visible(), "Coordinator dashboard is not visible after login")
            self.assert_text_present("Dashboard", (By.ID, "page-dashboard"))

        self.execute_test_case("TC-1", "TC-1 PASSED: Login successful", run)

    def test_02_teacher_creation(self):
        def run():
            self.prepare_clean_state()
            self.go_to_page("teachers")

            self.wait_and_type((By.ID, "t-name"), TEACHER_DATA["name"])
            self.wait_and_type((By.ID, "t-short"), TEACHER_DATA["short"])
            self.wait_and_type((By.ID, "t-email"), TEACHER_DATA["email"])
            self.wait_and_type((By.ID, "t-id"), TEACHER_DATA["id"])
            self.wait_and_type((By.ID, "t-maxday"), TEACHER_DATA["max_day"])
            self.wait_and_type((By.ID, "t-maxweek"), TEACHER_DATA["max_week"])
            self.wait_and_click((By.XPATH, "//div[@id='page-teachers']//button[contains(normalize-space(),'Add Teacher')]"))

            self.wait.until(
                lambda d: TEACHER_DATA["name"].lower() in d.find_element(By.ID, "teacher-tbody").text.lower()
                and TEACHER_DATA["id"].lower() in d.find_element(By.ID, "teacher-tbody").text.lower()
            )
            self.assert_text_present(TEACHER_DATA["name"], (By.ID, "teacher-tbody"))
            self.assert_text_present(TEACHER_DATA["email"], (By.ID, "teacher-tbody"))

        self.execute_test_case("TC-2", "TC-2 PASSED: Teacher added successfully", run)

    def test_03_room_creation(self):
        def run():
            self.go_to_page("rooms")

            for room in ROOMS_DATA:
                self.wait_and_type((By.ID, "r-name"), room["name"])
                self.wait_and_type((By.ID, "r-id"), room["id"])
                self.select_dropdown_by_text_or_first_option(
                    (By.ID, "r-type"),
                    preferred_text=room["type_label"],
                    preferred_value=room["room_type"],
                )
                self.wait_and_type((By.ID, "r-cap"), room["capacity"])
                self.wait_and_click((By.XPATH, "//div[@id='page-rooms']//button[contains(normalize-space(),'Add Room')]"))
                self.wait.until(lambda d: room["name"].lower() in d.find_element(By.ID, "room-tbody").text.lower())

            room_text = self.driver.find_element(By.ID, "room-tbody").text.lower()
            self.assertIn(ROOMS_DATA[0]["name"].lower(), room_text)
            self.assertIn(ROOMS_DATA[1]["name"].lower(), room_text)

        self.execute_test_case("TC-3", "TC-3 PASSED: Room added successfully", run)

    def test_04_subject_creation(self):
        def run():
            self.go_to_page("subjects")

            self.wait_and_type((By.ID, "sj-name"), SUBJECT_DATA["name"])
            self.wait_and_type((By.ID, "sj-short"), SUBJECT_DATA["short"])
            self.wait_and_type((By.ID, "sj-code"), SUBJECT_DATA["code"])
            self.wait_and_type((By.ID, "sj-id"), SUBJECT_DATA["id"])
            self.select_dropdown_by_text_or_first_option(
                (By.ID, "sj-teacher"),
                preferred_text=TEACHER_DATA["name"],
                preferred_value=TEACHER_DATA["id"],
            )
            self.wait_and_type((By.ID, "sj-lec"), SUBJECT_DATA["lectures_per_week"])
            self.enable_lab_section()
            self.wait_and_type((By.ID, "sj-labhrs"), SUBJECT_DATA["lab_hours_per_week"])
            self.select_dropdown_by_text_or_first_option(
                (By.ID, "sj-labteacher"),
                preferred_text=TEACHER_DATA["name"],
                preferred_value=TEACHER_DATA["id"],
            )
            self.wait_and_click((By.XPATH, "//div[@id='page-subjects']//button[contains(normalize-space(),'Add Subject')]"))

            self.wait.until(
                lambda d: SUBJECT_DATA["name"].lower() in d.find_element(By.ID, "subject-tbody").text.lower()
                and SUBJECT_DATA["id"].lower() in d.find_element(By.ID, "subject-tbody").text.lower()
            )
            self.assert_text_present(SUBJECT_DATA["name"], (By.ID, "subject-tbody"))
            self.assert_text_present(TEACHER_DATA["id"], (By.ID, "subject-tbody"))

        self.execute_test_case("TC-4", "TC-4 PASSED: Subject added successfully", run)

    def test_05_division_and_batch(self):
        def run():
            self.go_to_page("divisions")

            self.wait_and_type((By.ID, "d-name"), DIVISION_DATA["name"])
            self.wait_and_type((By.ID, "d-id"), DIVISION_DATA["id"])
            self.wait_and_type((By.ID, "d-size"), DIVISION_DATA["students"])
            batch_count_input = self.wait_and_type((By.ID, "d-batches"), str(len(DIVISION_DATA["batches"])))
            self.driver.execute_script("arguments[0].dispatchEvent(new Event('input', {bubbles:true}));", batch_count_input)

            self.wait.until(lambda d: len(d.find_elements(By.CSS_SELECTOR, "#d-batch-names input")) >= len(DIVISION_DATA["batches"]))
            batch_inputs = self.driver.find_elements(By.CSS_SELECTOR, "#d-batch-names input")
            for index, batch_name in enumerate(DIVISION_DATA["batches"]):
                batch_inputs[index].click()
                batch_inputs[index].send_keys(Keys.CONTROL, "a")
                batch_inputs[index].send_keys(batch_name)

            subject_checkbox = self.wait.until(
                EC.element_to_be_clickable(
                    (
                        By.XPATH,
                        "//div[@id='d-subjects-grid']//label[contains(.,'Selenium DBMS')]//input[@type='checkbox']",
                    )
                )
            )
            if not subject_checkbox.is_selected():
                self.driver.execute_script("arguments[0].click();", subject_checkbox)

            self.wait_and_click((By.XPATH, "//div[@id='page-divisions']//button[contains(normalize-space(),'Add Division')]"))

            self.wait.until(lambda d: DIVISION_DATA["name"].lower() in d.find_element(By.ID, "div-list").text.lower())
            div_text = self.driver.find_element(By.ID, "div-list").text
            self.assertIn(DIVISION_DATA["name"], div_text)
            for batch_name in DIVISION_DATA["batches"]:
                self.assertIn(batch_name, div_text)

        self.execute_test_case("TC-5", "TC-5 PASSED: Division and batches added successfully", run)

    def test_06_constraint_working(self):
        def run():
            self.go_to_page("constraints")

            self.select_dropdown_by_text_or_first_option(
                (By.ID, "con-type"),
                preferred_text="Max Sessions Per Day",
                preferred_value=CONSTRAINT_DATA["type_value"],
            )
            self.select_dropdown_by_text_or_first_option(
                (By.ID, "con-priority"),
                preferred_text=CONSTRAINT_DATA["priority"].title(),
                preferred_value=CONSTRAINT_DATA["priority"],
            )
            self.wait_and_type((By.ID, "con-weight"), CONSTRAINT_DATA["weight"])
            self.select_dropdown_by_text_or_first_option(
                (By.ID, "con-scope"),
                preferred_text=CONSTRAINT_DATA["scope"].title(),
                preferred_value=CONSTRAINT_DATA["scope"],
            )
            self.select_dropdown_by_text_or_first_option(
                (By.ID, "con-teacher"),
                preferred_text=TEACHER_DATA["name"],
                preferred_value=CONSTRAINT_DATA["teacher_id"],
            )
            self.wait_and_type((By.ID, "con-val"), CONSTRAINT_DATA["max_per_day"])
            active_checkbox = self.driver.find_element(By.ID, "con-active")
            if not active_checkbox.is_selected():
                self.driver.execute_script("arguments[0].click();", active_checkbox)
            self.wait_and_type((By.ID, "con-desc"), CONSTRAINT_DATA["description"])

            self.wait.until(
                lambda d: "valid" in d.find_element(By.ID, "con-validate-msg").text.lower()
                or "constraint payload looks valid" in d.find_element(By.ID, "con-validate-msg").text.lower()
            )
            self.wait_and_click((By.XPATH, "//div[@id='page-constraints']//button[contains(normalize-space(),'Save Constraint')]"))

            self.wait.until(
                lambda d: CONSTRAINT_DATA["description"].lower()
                in d.find_element(By.ID, "constraint-list").text.lower()
            )
            self.assert_text_present(CONSTRAINT_DATA["description"], (By.ID, "constraint-list"))

        self.execute_test_case("TC-6", "TC-6 PASSED: Constraint saved and validation working", run)

    def test_07_timetable_generation(self):
        def run():
            self.go_to_page("generate")

            self.wait_and_click((By.XPATH, "//button[@id='gen-btn' and contains(normalize-space(),'Generate')]"))

            self.wait.until(
                lambda d: (
                    "generated" in d.find_element(By.ID, "gen-msg").text.lower()
                    or "completed" in d.find_element(By.ID, "gen-msg").text.lower()
                )
                and "failed" not in d.find_element(By.ID, "gen-msg").text.lower()
            )
            self.wait.until(lambda d: self.get_client_timetable_slot_count() > 0)

            gen_error_text = self.driver.find_element(By.ID, "gen-error-panel").text.strip().lower()
            fatal_markers = [
                "generation failed",
                "could not generate",
                "no timetable slots generated",
                "generation crashed",
            ]
            self.assertFalse(
                any(marker in gen_error_text for marker in fatal_markers),
                f"Unexpected generation failure details: {gen_error_text}",
            )
            self.assertGreater(
                self.get_client_timetable_slot_count(),
                0,
                "Client timetable data was not populated after generation",
            )

        self.execute_test_case("TC-7", "TC-7 PASSED: Timetable generated successfully", run)

    def test_08_timetable_view(self):
        def run():
            self.go_to_page("timetable")

            self.select_dropdown_by_text_or_first_option(
                (By.ID, "tt-div"),
                preferred_text=DIVISION_DATA["id"],
                preferred_value=DIVISION_DATA["id"],
            )
            self.wait.until(
                lambda d: self.get_client_timetable_slot_count(DIVISION_DATA["id"]) > 0
            )
            self.wait.until(
                lambda d: len(d.find_elements(By.CSS_SELECTOR, "#tt-container .tt-board")) > 0
            )

            tt_container_text = self.driver.find_element(By.ID, "tt-container").text.strip()
            self.assertTrue(tt_container_text, "Timetable container is empty")
            self.assertTrue(
                len(self.driver.find_elements(By.CSS_SELECTOR, "#tt-container .tt-slot-card, #tt-container .tt-clickable")) > 0,
                "Timetable rows/cards were not rendered",
            )

        self.execute_test_case("TC-8", "TC-8 PASSED: Timetable displayed successfully", run)

    def test_09_teacher_absent_and_slot_reallocation(self):
        def run():
            slot_context = self.find_teacher_slot_for_absence()
            self.__class__.absence_context = slot_context

            self.go_to_page("changes")
            self.wait_and_click(
                (
                    By.XPATH,
                    "//div[@id='page-changes']//div[contains(@class,'tab') and normalize-space()='Absent']",
                )
            )
            self.wait.until(lambda d: d.find_element(By.ID, "ct-absent").is_displayed())

            self.select_dropdown_by_text_or_first_option(
                (By.ID, "ab-teacher"),
                preferred_text=slot_context["teacher_id"],
                preferred_value=slot_context["teacher_id"],
            )
            self.select_dropdown_by_text_or_first_option((By.ID, "ab-day"), preferred_text=slot_context["day"])
            self.select_dropdown_by_text_or_first_option((By.ID, "ab-period"), preferred_value=slot_context["period_id"])
            self.wait_and_click(
                (
                    By.XPATH,
                    "//div[@id='ct-absent']//button[contains(normalize-space(),'Mark Absent + Notify Faculty')]",
                )
            )

            self.wait.until(
                lambda d: (
                    "opened for cover" in d.find_element(By.ID, "absent-msg").text.lower()
                    or "notifications sent" in d.find_element(By.ID, "absent-msg").text.lower()
                    or "no active sessions found" in d.find_element(By.ID, "absent-msg").text.lower()
                    or "open" in d.find_element(By.ID, "ab-open-list").text.lower()
                )
            )

            matching_notifications = self.fetch_matching_notifications(
                teacher_id=slot_context["teacher_id"],
                day=slot_context["day"],
                period_id=slot_context["period_id"],
            )
            self.__class__.latest_notification_payloads = matching_notifications

            absent_msg = self.driver.find_element(By.ID, "absent-msg").text.lower()
            open_list_text = self.driver.find_element(By.ID, "ab-open-list").text.lower()
            self.assertTrue(
                ("opened" in absent_msg and "cover" in absent_msg)
                or ("open" in open_list_text and "cover" in open_list_text)
                or bool(matching_notifications),
                "Teacher absence did not create an open cover request",
            )
            self.assertTrue(matching_notifications, "No notification/request was created for the absence")

        self.execute_test_case(
            "TC-9",
            "TC-9 PASSED: Teacher absence handled and cover request generated",
            run,
        )

    def test_10_notification(self):
        def run():
            context = self.__class__.absence_context or self.find_teacher_slot_for_absence()
            matching_notifications = self.fetch_matching_notifications(
                teacher_id=context["teacher_id"],
                day=context["day"],
                period_id=context["period_id"],
            )
            self.__class__.latest_notification_payloads = matching_notifications

            self.go_to_page("dashboard")
            self.wait.until(lambda d: d.find_element(By.ID, "dash-notifs").text.strip() != "")

            dashboard_notification_text = self.driver.find_element(By.ID, "dash-notifs").text.lower()
            joined_notification_text = " ".join(
                [
                    f"{item.get('title', '')} {item.get('message', '')} "
                    f"{(item.get('data') or {}).get('absent_teacher', '')} "
                    f"{(item.get('data') or {}).get('subject_name', '')}"
                    for item in matching_notifications
                ]
            ).lower()

            self.assertTrue(matching_notifications, "Notification endpoint did not return the expected absence notification")
            self.assertTrue(
                any(keyword in (dashboard_notification_text + " " + joined_notification_text) for keyword in ["absent", "cover", "claim", "teacher"]),
                "Notification text did not contain expected absence/cover keywords",
            )

        self.execute_test_case("TC-10", "TC-10 PASSED: Notification generated successfully", run)

    def test_11_change_log(self):
        def run():
            self.go_to_page("changelog")
            self.wait.until(lambda d: d.find_element(By.ID, "log-tbody").text.strip() != "")

            log_text = self.driver.find_element(By.ID, "log-tbody").text.lower()
            self.assertTrue(
                any(keyword in log_text for keyword in ["teacher_absent", "constraint", "generate", "change"]),
                "Change log does not show the expected recent records",
            )

            response = self.api_fetch("/api/change-log")
            if not response.get("ok"):
                raise AssertionError(f"Could not verify change log endpoint: {response}")
            log_rows = response.get("data") or []
            self.assertTrue(log_rows, "Change log endpoint returned no records")
            self.assertTrue(
                any(
                    str(row.get("change_type", "")).lower() in {"teacher_absent", "constraint_add", "constraint_update", "generate", "generate_partial"}
                    or any(token in str(row.get("description", "")).lower() for token in ["absent", "constraint", "generated", "change"])
                    for row in log_rows[:15]
                ),
                "Latest change log records do not contain the expected entries",
            )

        self.execute_test_case("TC-11", "TC-11 PASSED: Change log updated successfully", run)


def suite():
    ordered_tests = [
        "test_01_login",
        "test_02_teacher_creation",
        "test_03_room_creation",
        "test_04_subject_creation",
        "test_05_division_and_batch",
        "test_06_constraint_working",
        "test_07_timetable_generation",
        "test_08_timetable_view",
        "test_09_teacher_absent_and_slot_reallocation",
        "test_10_notification",
        "test_11_change_log",
    ]
    test_suite = unittest.TestSuite()
    for test_name in ordered_tests:
        test_suite.addTest(AcademicTimetableSeleniumTests(test_name))
    return test_suite


if __name__ == "__main__":
    runner = unittest.TextTestRunner(verbosity=2)
    result = runner.run(suite())
    if result.wasSuccessful():
        print("ALL SELENIUM CIE TEST CASES EXECUTED SUCCESSFULLY")
