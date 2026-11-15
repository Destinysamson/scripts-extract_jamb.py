import json
import os
import re
import sys
import time
from pathlib import Path
from datetime import datetime, timezone
from urllib.parse import quote

import requests

# =====================================
# STALWART PREP CONFIGURATION
# =====================================

BASE_URL = os.getenv(
    "ALOC_BASE_URL",
    "https://dev.aloc.com.ng/api/v1"
).rstrip("/")

API_KEY = os.getenv("ALOC_API_KEY", "").strip()

YEAR_SCOPE = os.getenv(
    "YEAR_SCOPE", "priority"
).lower()

MAX_SUBJECTS = int(
    os.getenv("MAX_SUBJECTS", "0")
)

# Recommended JAMB coverage
PRIORITY_YEARS = list(
    range(2026, 2014, -1)
)

ARCHIVE_YEARS = (
    PRIORITY_YEARS
    + list(range(2014, 2009, -1))
    + list(range(2009, 1999, -1))
)

DATABASE = Path("database")
REPORTS = Path("reports")

DATABASE.mkdir(exist_ok=True)
REPORTS.mkdir(exist_ok=True)

# =====================================
# API CONNECTION
# =====================================

session = requests.Session()

session.headers.update({
    "X-API-Key": API_KEY,
    "Accept": "application/json",
    "User-Agent": "StalwartPrep/1.0"
})


def request_api(endpoint, params=None):

    url = BASE_URL + endpoint

    for attempt in range(6):

        try:

            response = session.get(
                url,
                params=params,
                timeout=45
            )

            if response.status_code in [401, 403, 406]:

                raise RuntimeError(
                    f"ALOC authentication/request error: "
                    f"{response.status_code}"
                )

            if (
                response.status_code == 429
                or response.status_code >= 500
            ):

                time.sleep(min(60, 2 ** attempt))
                continue

            response.raise_for_status()

            return response.json()

        except (
            requests.Timeout,
            requests.ConnectionError
        ):

            time.sleep(min(60, 2 ** attempt))

    raise RuntimeError(
        f"API request failed: {endpoint}"
    )


def get_data(payload, description):

    if not isinstance(payload, dict):

        raise RuntimeError(
            f"Invalid response: {description}"
        )

    data = payload.get("data")

    if not isinstance(data, list):

        raise RuntimeError(
            f"Missing data array: {description}"
        )

    return data


# =====================================
# DISCOVER ALL AVAILABLE SUBJECTS
# =====================================

def discover_subjects():

    payload = request_api("/subjects")

    records = get_data(
        payload,
        "subject catalogue"
    )

    subjects = []
    seen = set()

    for item in records:

        if not isinstance(item, dict):
            continue

        name = (
            item.get("name")
            or item.get("slug")
            or item.get("code")
        )

        if not name:
            continue

        name = str(name).strip().lower()

        if name in seen:
            continue

        seen.add(name)

        subjects.append({
            "name": name,
            "displayName": (
                item.get("displayName")
                or name.replace("-", " ").title()
            ),
            "category": item.get("category"),
            "code": item.get("code")
        })

    return subjects


# =====================================
# DISCOVER AVAILABLE JAMB YEARS
# =====================================

def discover_years(subject):

    endpoint = (
        "/subjects/"
        + quote(subject, safe="")
        + "/years"
    )

    payload = request_api(endpoint)

    records = get_data(
        payload,
        f"{subject} years"
    )

    years = set()

    for item in records:

        if not isinstance(item, dict):
            continue

        year = item.get("year")

        if year is None:
            continue

        try:
            year = int(year)
        except (ValueError, TypeError):
            continue

        exam_types = [
            str(x).lower()
            for x in item.get("examTypes", [])
        ]

        breakdown = item.get("breakdown") or {}

        jamb_count = breakdown.get("jamb", 0)

        if (
            "jamb" in exam_types
            or (
                isinstance(jamb_count, (int, float))
                and jamb_count > 0
            )
        ):

            if 2000 <= year <= 2026:
                years.add(year)

    return years


# =====================================
# EXTRACT QUESTIONS WITH PAGINATION
# =====================================

def extract_questions(subject, year):

    questions = []

    cursor = None
    seen_cursors = set()

    while True:

        params = {
            "subject": subject,
            "examType": "jamb",
            "year": year,
            "contentSource": "historical",
            "limit": 15
        }

        if cursor:
            params["cursor"] = cursor

        payload = request_api(
            "/questions",
            params
        )

        records = get_data(
            payload,
            f"{subject} {year}"
        )

        for record in records:

            if not isinstance(record, dict):
                continue

            question = dict(record)

            question["_stalwartPrep"] = {
                "examType": "jamb",
                "subject": subject,
                "year": year,
                "source": "ALOC historical",
                "retrievedAt": datetime.now(
                    timezone.utc
                ).isoformat()
            }

            questions.append(question)

        pagination = payload.get("pagination") or {}

        has_more = pagination.get("hasMore", False)

        next_cursor = pagination.get("nextCursor")

        if not has_more:
            break

        if not next_cursor:
            raise RuntimeError(
                f"Missing next cursor: {subject} {year}"
            )

        if next_cursor in seen_cursors:
            raise RuntimeError(
                f"Repeated cursor: {subject} {year}"
            )

        seen_cursors.add(next_cursor)

        cursor = next_cursor

        # Respect API request limits
        time.sleep(2.2)

    # Remove duplicate question IDs
    unique = {}
    no_id = []

    for question in questions:

        qid = question.get("id")

        if qid:
            unique[str(qid)] = question
        else:
            no_id.append(question)

    return list(unique.values()) + no_id


# =====================================
# MAIN EXTRACTION PROCESS
# =====================================

def main():

    if not API_KEY:
        raise RuntimeError(
            "ALOC_SECRET_KEY was not supplied to the script."
        )

    if YEAR_SCOPE not in ["priority", "archive"]:
        raise RuntimeError(
            "Invalid year scope."
        )

    years_order = (
        PRIORITY_YEARS
        if YEAR_SCOPE == "priority"
        else ARCHIVE_YEARS
    )

    print("STALWART PREP JAMB EXTRACTION")
    print("Year scope:", YEAR_SCOPE)

    subjects = discover_subjects()

    if MAX_SUBJECTS > 0:
        subjects = subjects[:MAX_SUBJECTS]

    if not subjects:
        raise RuntimeError(
            "No subjects found in ALOC catalogue."
        )

    successful = []
    empty = []
    failed = []

    total_questions = 0

    totals_by_subject = {}

    # =================================
    # PROCESS EVERY SUBJECT
    # =================================

    for subject_info in subjects:

        subject = subject_info["name"]

        print("\n================================")
        print("SUBJECT:", subject)
        print("================================")

        try:

            available_years = discover_years(subject)

        except Exception as error:

            failed.append({
                "subject": subject,
                "stage": "year_discovery",
                "error": str(error)
            })

            continue

        years = [
            year
            for year in years_order
            if year in available_years
        ]

        if not years:

            empty.append({
                "subject": subject,
                "reason": "No supported JAMB years found."
            })

            continue

        subject_questions = []

        for year in years:

            print(
                f"Extracting {subject} - {year}"
            )

            try:

                records = extract_questions(
                    subject,
                    year
                )

                if records:

                    subject_questions.extend(records)

                    successful.append({
                        "subject": subject,
                        "year": year,
                        "questions": len(records)
                    })

                    total_questions += len(records)

                else:

                    empty.append({
                        "subject": subject,
                        "year": year,
                        "reason": "No questions returned."
                    })

            except Exception as error:

                failed.append({
                    "subject": subject,
                    "year": year,
                    "error": str(error)
                })

                print(
                    "FAILED:",
                    subject,
                    year,
                    error
                )

        # =================================
        # SAVE REAL QUESTION CONTENT
        # =================================

        if subject_questions:

            filename = re.sub(
                r"[^a-zA-Z0-9]+",
                "-",
                subject
            ).strip("-").lower()

            output_file = (
                DATABASE / f"{filename}.json"
            )

            output_file.write_text(
                json.dumps(
                    subject_questions,
                    ensure_ascii=False,
                    indent=2
                ),
                encoding="utf-8"
            )

            totals_by_subject[subject] = len(
                subject_questions
            )

            print(
                "SAVED:",
                output_file,
                len(subject_questions),
                "questions"
            )

    # =================================
    # SAVE EXTRACTION REPORT
    # =================================

    report = {

        "project": "Stalwart Prep",

        "examType": "JAMB",

        "stage": YEAR_SCOPE,

        "yearsRequested": years_order,

        "subjectsDiscovered": subjects,

        "successful": successful,

        "empty": empty,

        "failed": failed,

        "total_questions": total_questions,

        "totalsBySubject": totals_by_subject,

        "createdAt": datetime.now(
            timezone.utc
        ).isoformat(),

        "notes": [
            "Questions are retrieved from ALOC historical source.",
            "Original question records are preserved.",
            "Missing answers are not invented.",
            "Actual question data is saved in database/.",
            "Extraction report is saved in reports/."
        ]
    }

    report_file = (
        REPORTS / "extraction-report.json"
    )

    report_file.write_text(
        json.dumps(
            report,
            ensure_ascii=False,
            indent=2
        ),
        encoding="utf-8"
    )

    print("\n================================")
    print("EXTRACTION COMPLETED")
    print("================================")

    print("Total questions:", total_questions)

    print("Successful batches:", len(successful))

    print("Empty batches:", len(empty))

    print("Failed batches:", len(failed))

    print("Database folder:", DATABASE)

    print("Report:", report_file)

    # Prevent a report-only successful export

    if total_questions == 0:

        raise RuntimeError(
            "ZERO QUESTIONS SAVED. "
            "Check API authentication and endpoint responses."
        )


if __name__ == "__main__":

    try:
        main()

    except Exception as error:

        print("FATAL ERROR:", error)

        sys.exit(1)
