import json
import re
import time
import unicodedata
from pathlib import Path
from collections import Counter

import requests


# ============================================================
# Fyucha Player Database - Wikidata V2.2.8
#
# V2.2.8
#
# MAIN PURPOSE:
#   Diagnose and stabilize the Wikidata API/search layer.
#
# PRESERVED:
#   - Conservative identity matching
#   - SAFE / REVIEW / REJECT classification
#   - Conservative DOB correction
#   - Year-conflict protection
#   - Wikidata precision handling
#
# NEW:
#   - Search API error classification
#   - Entity API error classification
#   - HTTP status diagnostics
#   - Retry isolation
#   - Search-result caching
#   - Search-query deduplication
#   - Fallback provenance
#   - Candidate diagnostics
#   - Score-gap diagnostics
#   - API diagnostics report
#   - Search diagnostics report
# ============================================================


VERSION = "2.2.8"


# ============================================================
# FILES
# ============================================================

INPUT_FILE = Path("output/players.json")
OUTPUT_DIR = Path("output")

ENRICHED_FILE = OUTPUT_DIR / "enriched-players-test.json"
MATCHES_FILE = OUTPUT_DIR / "wikidata-matches-test.json"
CORRECTIONS_FILE = OUTPUT_DIR / "dob-corrections-test.json"
CONFLICTS_FILE = OUTPUT_DIR / "dob-conflicts-test.json"
YEAR_ONLY_FILE = OUTPUT_DIR / "year-only-test.json"
ERRORS_FILE = OUTPUT_DIR / "wikidata-errors-test.json"

API_DIAGNOSTICS_FILE = OUTPUT_DIR / "wikidata-api-diagnostics-test.json"
SEARCH_DIAGNOSTICS_FILE = OUTPUT_DIR / "wikidata-search-diagnostics-test.json"

DECEASED_FILE = OUTPUT_DIR / "deceased-test.json"


# ============================================================
# TEST SETTINGS
# ============================================================

TEST_LIMIT = 100

SEARCH_LIMIT = 10

MAX_RETRIES = 4

BASE_DELAY = 1.0

REQUEST_DELAY = 0.10


# ============================================================
# WIKIDATA API
# ============================================================

SEARCH_URL = "https://www.wikidata.org/w/api.php"

ENTITY_URL = "https://www.wikidata.org/wiki/Special:EntityData/{}.json"


HEADERS = {
    "User-Agent": (
        "FyuchaPlayerDatabase/"
        + VERSION
        + " (football player DOB research; "
        "contact via fyuchafc.com)"
    )
}


# ============================================================
# GLOBAL CACHES
# ============================================================

ENTITY_CACHE = {}

SEARCH_CACHE = {}

SEARCH_QUERY_CACHE = set()


# ============================================================
# DIAGNOSTICS
# ============================================================

API_DIAGNOSTICS = {
    "total_requests": 0,
    "successful_requests": 0,

    "search_requests": 0,
    "search_successes": 0,

    "entity_requests": 0,
    "entity_successes": 0,

    "retries": 0,

    "errors": 0,

    "status_codes": Counter(),

    "error_types": Counter(),

    "search_error_types": Counter(),
    "entity_error_types": Counter(),

    "search_status_codes": Counter(),
    "entity_status_codes": Counter(),

    "transient_errors": 0,
    "permanent_errors": 0,

    "timeout_errors": 0,
    "connection_errors": 0,

    "rate_limit_errors": 0,
    "server_errors": 0,
    "client_errors": 0,
}


SEARCH_DIAGNOSTICS = {
    "total_queries": 0,
    "unique_queries": 0,
    "cached_queries": 0,

    "primary_queries": 0,
    "fallback_queries": 0,

    "primary_matches": 0,
    "fallback_matches": 0,

    "primary_api_errors": 0,
    "fallback_api_errors": 0,

    "query_errors": [],

    "fallback_attempts": [],

    "candidate_diagnostics": [],

    "score_gap_diagnostics": [],
}


# ============================================================
# BASIC HELPERS
# ============================================================

def ensure_output_dir():
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)


def write_json(path, data):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(
            data,
            f,
            ensure_ascii=False,
            indent=2
        )


def load_json(path):
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


# ============================================================
# NAME NORMALIZATION
# ============================================================

def normalize_name(value):
    if not value:
        return ""

    value = str(value)

    value = unicodedata.normalize(
        "NFKD",
        value
    )

    value = "".join(
        c for c in value
        if not unicodedata.combining(c)
    )

    value = value.lower()

    value = value.replace("-", " ")
    value = value.replace("_", " ")

    value = re.sub(
        r"[^a-z0-9\s]",
        " ",
        value
    )

    value = re.sub(
        r"\s+",
        " ",
        value
    )

    return value.strip()


def compact_name(value):
    return normalize_name(value).replace(" ", "")


def name_tokens(value):
    return set(
        normalize_name(value).split()
    )


# ============================================================
# YEAR EXTRACTION
# ============================================================

def extract_year(value):
    if not value:
        return None

    match = re.search(
        r"(19\d{2}|20\d{2})",
        str(value)
    )

    if not match:
        return None

    return int(match.group(1))


# ============================================================
# SOURCE DOB PRECISION
# ============================================================

def get_source_precision(value):
    """
    Determines source precision.

    YYYY-01-01 is treated as YEAR precision.

    YYYY-MM-01 is treated as MONTH precision.

    YYYY-MM-DD is DAY precision.
    """

    if not value:
        return "unknown"

    value = str(value).strip()

    if re.fullmatch(
        r"(19|20)\d{2}",
        value
    ):
        return "year"

    if re.fullmatch(
        r"(19|20)\d{2}-\d{2}",
        value
    ):
        return "month"

    if re.fullmatch(
        r"(19|20)\d{2}-\d{2}-\d{2}",
        value
    ):
        year = int(value[:4])
        month = int(value[5:7])
        day = int(value[8:10])

        if month == 1 and day == 1:
            return "year"

        if day == 1:
            return "month"

        return "day"

    return "unknown"


# ============================================================
# WIKIDATA DOB PRECISION
# ============================================================

def wikidata_precision_to_text(precision):
    """
    Wikibase time precision:
        9  = year
        10 = month
        11 = day
    """

    try:
        precision = int(precision)
    except Exception:
        return "unknown"

    if precision == 9:
        return "year"

    if precision == 10:
        return "month"

    if precision == 11:
        return "day"

    return "unknown"


# ============================================================
# WIKIDATA DOB EXTRACTION
# ============================================================

def extract_dob(entity):
    claims = (
        entity
        .get("claims", {})
        .get("P569", [])
    )

    records = []

    for claim in claims:

        try:
            value = claim["mainsnak"]["datavalue"]["value"]

            time_value = value.get("time")

            precision = value.get("precision")

            if not time_value:
                continue

            time_value = time_value.lstrip("+")

            date_match = re.match(
                r"(\d{4})-(\d{2})-(\d{2})",
                time_value
            )

            if not date_match:
                continue

            year = int(date_match.group(1))
            month = int(date_match.group(2))
            day = int(date_match.group(3))

            precision_text = (
                wikidata_precision_to_text(
                    precision
                )
            )

            records.append({
                "date": f"{year:04d}-{month:02d}-{day:02d}",
                "year": year,
                "month": month,
                "day": day,
                "precision": precision_text
            })

        except Exception:
            continue

    if not records:
        return None

    # Prefer the highest precision.
    order = {
        "day": 3,
        "month": 2,
        "year": 1,
        "unknown": 0
    }

    records.sort(
        key=lambda x: order.get(
            x["precision"],
            0
        ),
        reverse=True
    )

    return records[0]


# ============================================================
# FOOTBALL RELEVANCE
# ============================================================

FOOTBALL_TERMS = {
    "footballer",
    "football player",
    "soccer player",
    "football",
    "soccer",
    "midfielder",
    "defender",
    "forward",
    "goalkeeper",
    "striker",
    "winger",
    "manager",
    "coach",
}


def is_football_related(entity):
    description = (
        entity
        .get("descriptions", {})
        .get("en", {})
        .get("value", "")
        .lower()
    )

    labels = (
        entity
        .get("labels", {})
        .get("en", {})
        .get("value", "")
        .lower()
    )

    text = description + " " + labels

    return any(
        term in text
        for term in FOOTBALL_TERMS
    )


# ============================================================
# NAME SCORING
# ============================================================

def calculate_name_score(source_name, candidate_name):

    source_norm = normalize_name(source_name)
    candidate_norm = normalize_name(candidate_name)

    if not source_norm or not candidate_norm:
        return 0

    if source_norm == candidate_norm:
        return 100

    if compact_name(source_name) == compact_name(candidate_name):
        return 98

    source_tokens = name_tokens(source_name)
    candidate_tokens = name_tokens(candidate_name)

    if (
        source_tokens
        and source_tokens == candidate_tokens
    ):
        return 96

    overlap = (
        len(source_tokens & candidate_tokens)
        /
        max(
            len(source_tokens),
            len(candidate_tokens),
            1
        )
    )

    if overlap >= 0.8:
        return 90

    if overlap >= 0.66:
        return 80

    if overlap >= 0.5:
        return 70

    return 0


# ============================================================
# CANDIDATE CREATION
# ============================================================

def build_candidate_from_entity(
    entity,
    source_name,
    source_year
):

    qid = entity.get("id")

    label = (
        entity
        .get("labels", {})
        .get("en", {})
        .get("value", "")
    )

    description = (
        entity
        .get("descriptions", {})
        .get("en", {})
        .get("value", "")
    )

    dob = extract_dob(entity)

    candidate_year = (
        dob["year"]
        if dob
        else None
    )

    name_score = calculate_name_score(
        source_name,
        label
    )

    football = is_football_related(
        entity
    )

    same_year = (
        source_year is not None
        and candidate_year is not None
        and source_year == candidate_year
    )

    different_year = (
        source_year is not None
        and candidate_year is not None
        and source_year != candidate_year
    )

    identity_score = name_score

    if football:
        identity_score += 15

    if same_year:
        identity_score += 10

    if different_year:
        identity_score -= 5

    return {
        "wikidataId": qid,
        "matchedName": label,
        "description": description,
        "nameScore": name_score,
        "footballRelated": football,
        "candidateYear": candidate_year,
        "sameYear": same_year,
        "differentYear": different_year,
        "identityScore": identity_score,
        "wikidataDob": (
            dob["date"]
            if dob
            else None
        ),
        "wikidataDobPrecision": (
            dob["precision"]
            if dob
            else None
        ),
        "_entity": entity,
    }


# ============================================================
# BEST MATCH
# ============================================================

def select_best_match(
    candidates
):

    if not candidates:
        return None, []

    candidates = sorted(
        candidates,
        key=lambda x: (
            x["identityScore"],
            x["nameScore"],
            int(x["footballRelated"]),
            int(x["sameYear"]),
        ),
        reverse=True
    )

    best = candidates[0]

    second = (
        candidates[1]
        if len(candidates) > 1
        else None
    )

    score_gap = (
        best["identityScore"]
        -
        second["identityScore"]
        if second
        else None
    )

    SEARCH_DIAGNOSTICS[
        "score_gap_diagnostics"
    ].append({
        "wikidataId": best.get(
            "wikidataId"
        ),
        "bestScore": best.get(
            "identityScore"
        ),
        "secondScore": (
            second.get("identityScore")
            if second
            else None
        ),
        "scoreGap": score_gap,
    })

    return best, candidates


# ============================================================
# IDENTITY CLASSIFICATION
# ============================================================

def classify_identity(candidate):

    if not candidate:
        return "REJECT"

    name_score = candidate["nameScore"]

    football = candidate["footballRelated"]

    same_year = candidate["sameYear"]

    different_year = candidate["differentYear"]

    # --------------------------------------------------------
    # SAFE
    # --------------------------------------------------------

    if (
        name_score >= 94
        and football
    ):
        return "SAFE"

    if (
        name_score >= 94
        and same_year
    ):
        return "SAFE"

    if (
        name_score >= 78
        and football
        and same_year
    ):
        return "SAFE"

    # --------------------------------------------------------
    # REJECT YEAR-CONFLICTING WEAK MATCHES
    # --------------------------------------------------------

    if (
        different_year
        and name_score < 94
    ):
        return "REJECT"

    # --------------------------------------------------------
    # REVIEW
    # --------------------------------------------------------

    if name_score >= 78:
        return "REVIEW"

    if (
        name_score >= 65
        and same_year
    ):
        return "REVIEW"

    return "REJECT"


# ============================================================
# DOB EVALUATION
# ============================================================

def evaluate_dob(
    source_dob,
    candidate,
    identity
):

    result = {
        "action": "KEEP_SOURCE",
        "reason": "",
        "sourceDateOfBirth": source_dob,
        "sourcePrecision": get_source_precision(
            source_dob
        ),
        "wikidataDateOfBirth": (
            candidate.get("wikidataDob")
            if candidate
            else None
        ),
        "wikidataPrecision": (
            candidate.get("wikidataDobPrecision")
            if candidate
            else None
        ),
    }

    if not candidate:
        result["reason"] = "No Wikidata candidate"
        return result

    wikidata_dob = candidate.get(
        "wikidataDob"
    )

    if not wikidata_dob:
        result["reason"] = "Wikidata DOB unavailable"
        return result

    source_precision = get_source_precision(
        source_dob
    )

    wikidata_precision = candidate.get(
        "wikidataDobPrecision"
    )

    source_year = extract_year(
        source_dob
    )

    wikidata_year = extract_year(
        wikidata_dob
    )

    # --------------------------------------------------------
    # NEVER AUTO-CORRECT YEAR CONFLICT
    # --------------------------------------------------------

    if (
        source_year
        and wikidata_year
        and source_year != wikidata_year
    ):

        result["action"] = "CONFLICT"

        result["reason"] = (
            "Source year differs from Wikidata year"
        )

        return result

    # --------------------------------------------------------
    # YEAR ONLY -> FULL DAY
    # --------------------------------------------------------

    if (
        source_precision == "year"
        and identity == "SAFE"
        and wikidata_precision == "day"
        and source_year == wikidata_year
    ):

        result["action"] = "CORRECT"

        result["reason"] = (
            "SAFE identity with precise Wikidata day"
        )

        return result

    # --------------------------------------------------------
    # MONTH -> FULL DAY
    # --------------------------------------------------------

    if (
        source_precision == "month"
        and identity == "SAFE"
        and wikidata_precision == "day"
    ):

        source_month = (
            str(source_dob)[5:7]
            if len(str(source_dob)) >= 7
            else None
        )

        wiki_month = wikidata_dob[5:7]

        if (
            source_year == wikidata_year
            and source_month == wiki_month
        ):

            result["action"] = "CORRECT"

            result["reason"] = (
                "SAFE identity with matching month "
                "and precise Wikidata day"
            )

            return result

    # --------------------------------------------------------
    # FULL DAY AGREEMENT
    # --------------------------------------------------------

    if (
        source_precision == "day"
        and wikidata_precision == "day"
        and source_dob == wikidata_dob
    ):

        result["action"] = "MATCH"

        result["reason"] = (
            "Source and Wikidata DOB agree"
        )

        return result

    # --------------------------------------------------------
    # FULL DAY DISAGREEMENT
    # --------------------------------------------------------

    if (
        source_precision == "day"
        and wikidata_precision == "day"
        and source_year == wikidata_year
        and source_dob != wikidata_dob
    ):

        result["action"] = "CONFLICT"

        result["reason"] = (
            "Same birth year but different full DOB"
        )

        return result

    result["reason"] = (
        "No conservative correction condition met"
    )

    return result


# ============================================================
# SEARCH QUERY BUILDER
# ============================================================

def build_search_queries(name):

    original = str(name).strip()

    normalized = normalize_name(name)

    queries = []

    def add(query):
        query = query.strip()

        if query and query not in queries:
            queries.append(query)

    # Primary
    add(original)

    # Normalized
    if normalized:
        add(normalized)

    # Football-specific fallback queries
    if original:
        add(
            f"{original} footballer"
        )

        add(
            f"{original} football player"
        )

        add(
            f"{original} soccer player"
        )

    # Conservative first-name + surname query
    tokens = normalized.split()

    if len(tokens) >= 2:
        add(
            f"{tokens[0]} {tokens[-1]}"
        )

    return queries


# ============================================================
# ERROR CLASSIFICATION
# ============================================================

def classify_http_status(status):

    if status is None:
        return "unknown"

    if status == 429:
        return "rate_limit"

    if 500 <= status <= 599:
        return "server_error"

    if 400 <= status <= 499:
        return "client_error"

    if 200 <= status <= 299:
        return "success"

    return "other"


def is_retryable_status(status):

    if status == 429:
        return True

    if status is not None and 500 <= status <= 599:
        return True

    return False


def record_api_error(
    endpoint_type,
    status,
    error_type,
    query=None,
    attempt=None,
    message=None
):

    API_DIAGNOSTICS["errors"] += 1

    if status is not None:
        API_DIAGNOSTICS[
            "status_codes"
        ][str(status)] += 1

    API_DIAGNOSTICS[
        "error_types"
    ][error_type] += 1

    if endpoint_type == "search":

        API_DIAGNOSTICS[
            "search_error_types"
        ][error_type] += 1

        if status is not None:
            API_DIAGNOSTICS[
                "search_status_codes"
            ][str(status)] += 1

    elif endpoint_type == "entity":

        API_DIAGNOSTICS[
            "entity_error_types"
        ][error_type] += 1

        if status is not None:
            API_DIAGNOSTICS[
                "entity_status_codes"
            ][str(status)] += 1

    if error_type == "rate_limit":
        API_DIAGNOSTICS[
            "rate_limit_errors"
        ] += 1

    elif error_type == "server_error":
        API_DIAGNOSTICS[
            "server_errors"
        ] += 1

    elif error_type == "client_error":
        API_DIAGNOSTICS[
            "client_errors"
        ] += 1

    elif error_type == "timeout":
        API_DIAGNOSTICS[
            "timeout_errors"
        ] += 1

    elif error_type == "connection":
        API_DIAGNOSTICS[
            "connection_errors"
        ] += 1

    if (
        status is not None
        and is_retryable_status(status)
    ):
        API_DIAGNOSTICS[
            "transient_errors"
        ] += 1
    else:
        API_DIAGNOSTICS[
            "permanent_errors"
        ] += 1


# ============================================================
# REQUEST WITH CONTROLLED RETRIES
# ============================================================

def request_json(
    url,
    params=None,
    endpoint_type="unknown",
    query=None
):

    for attempt in range(
        1,
        MAX_RETRIES + 1
    ):

        API_DIAGNOSTICS[
            "total_requests"
        ] += 1

        if endpoint_type == "search":
            API_DIAGNOSTICS[
                "search_requests"
            ] += 1

        elif endpoint_type == "entity":
            API_DIAGNOSTICS[
                "entity_requests"
            ] += 1

        try:

            response = requests.get(
                url,
                params=params,
                headers=HEADERS,
                timeout=20
            )

            status = response.status_code

            if 200 <= status <= 299:

                API_DIAGNOSTICS[
                    "successful_requests"
                ] += 1

                if endpoint_type == "search":
                    API_DIAGNOSTICS[
                        "search_successes"
                    ] += 1

                elif endpoint_type == "entity":
                    API_DIAGNOSTICS[
                        "entity_successes"
                    ] += 1

                time.sleep(
                    REQUEST_DELAY
                )

                return {
                    "ok": True,
                    "data": response.json(),
                    "status": status,
                    "errorType": None,
                    "attempts": attempt,
                }

            error_type = classify_http_status(
                status
            )

            record_api_error(
                endpoint_type=endpoint_type,
                status=status,
                error_type=error_type,
                query=query,
                attempt=attempt,
                message=response.text[:500]
            )

            # ------------------------------------------------
            # Permanent error
            # ------------------------------------------------

            if not is_retryable_status(
                status
            ):

                return {
                    "ok": False,
                    "data": None,
                    "status": status,
                    "errorType": error_type,
                    "attempts": attempt,
                }

            # ------------------------------------------------
            # Retry transient error
            # ------------------------------------------------

            if attempt < MAX_RETRIES:

                API_DIAGNOSTICS[
                    "retries"
                ] += 1

                delay = (
                    BASE_DELAY
                    * (2 ** (attempt - 1))
                )

                if status == 429:
                    delay += 1.0

                time.sleep(delay)

        except requests.exceptions.Timeout:

            record_api_error(
                endpoint_type=endpoint_type,
                status=None,
                error_type="timeout",
                query=query,
                attempt=attempt
            )

            if attempt < MAX_RETRIES:

                API_DIAGNOSTICS[
                    "retries"
                ] += 1

                time.sleep(
                    BASE_DELAY
                    * (2 ** (attempt - 1))
                )

        except requests.exceptions.ConnectionError:

            record_api_error(
                endpoint_type=endpoint_type,
                status=None,
                error_type="connection",
                query=query,
                attempt=attempt
            )

            if attempt < MAX_RETRIES:

                API_DIAGNOSTICS[
                    "retries"
                ] += 1

                time.sleep(
                    BASE_DELAY
                    * (2 ** (attempt - 1))
                )

        except requests.exceptions.RequestException as exc:

            record_api_error(
                endpoint_type=endpoint_type,
                status=None,
                error_type="request_exception",
                query=query,
                attempt=attempt,
                message=str(exc)
            )

            return {
                "ok": False,
                "data": None,
                "status": None,
                "errorType": "request_exception",
                "attempts": attempt,
            }

        except ValueError:

            record_api_error(
                endpoint_type=endpoint_type,
                status=status
                if "status" in locals()
                else None,
                error_type="invalid_json",
                query=query,
                attempt=attempt
            )

            return {
                "ok": False,
                "data": None,
                "status": status
                if "status" in locals()
                else None,
                "errorType": "invalid_json",
                "attempts": attempt,
            }

    return {
        "ok": False,
        "data": None,
        "status": None,
        "errorType": "retry_exhausted",
        "attempts": MAX_RETRIES,
    }


# ============================================================
# SEARCH WIKIDATA
# ============================================================

def search_wikidata(
    query,
    limit=SEARCH_LIMIT,
    pass_name="primary"
):

    normalized_query = query.strip()

    SEARCH_DIAGNOSTICS[
        "total_queries"
    ] += 1

    if pass_name == "primary":
        SEARCH_DIAGNOSTICS[
            "primary_queries"
        ] += 1
    else:
        SEARCH_DIAGNOSTICS[
            "fallback_queries"
        ] += 1

    # --------------------------------------------------------
    # SEARCH CACHE
    # --------------------------------------------------------

    cache_key = (
        normalized_query.lower(),
        limit
    )

    if cache_key in SEARCH_CACHE:

        SEARCH_DIAGNOSTICS[
            "cached_queries"
        ] += 1

        return SEARCH_CACHE[
            cache_key
        ]

    SEARCH_DIAGNOSTICS[
        "unique_queries"
    ] += 1

    params = {
        "action": "wbsearchentities",
        "search": normalized_query,
        "language": "en",
        "uselang": "en",
        "format": "json",
        "limit": limit,
    }

    result = request_json(
        SEARCH_URL,
        params=params,
        endpoint_type="search",
        query=normalized_query
    )

    if not result["ok"]:

        if pass_name == "primary":
            SEARCH_DIAGNOSTICS[
                "primary_api_errors"
            ] += 1
        else:
            SEARCH_DIAGNOSTICS[
                "fallback_api_errors"
            ] += 1

        diagnostic = {
            "query": normalized_query,
            "pass": pass_name,
            "status": result.get(
                "status"
            ),
            "errorType": result.get(
                "errorType"
            ),
            "attempts": result.get(
                "attempts"
            ),
        }

        SEARCH_DIAGNOSTICS[
            "query_errors"
        ].append(
            diagnostic
        )

        SEARCH_CACHE[
            cache_key
        ] = []

        return []

    data = result["data"]

    search_results = data.get(
        "search",
        []
    )

    SEARCH_CACHE[
        cache_key
    ] = search_results

    return search_results


# ============================================================
# FETCH ENTITY
# ============================================================

def get_entity(qid):

    if not qid:
        return None

    if qid in ENTITY_CACHE:
        return ENTITY_CACHE[qid]

    url = ENTITY_URL.format(qid)

    result = request_json(
        url,
        endpoint_type="entity",
        query=qid
    )

    if not result["ok"]:
        return None

    try:

        entity = (
            result["data"]
            .get("entities", {})
            .get(qid)
        )

    except Exception:
        entity = None

    if entity:

        ENTITY_CACHE[qid] = entity

    return entity


# ============================================================
# DISCOVER CANDIDATES
# ============================================================

def discover_candidates(
    player_name,
    source_year
):

    queries = build_search_queries(
        player_name
    )

    all_candidates = {}

    used_queries = []

    search_pass = "primary"

    primary_query = (
        queries[0]
        if queries
        else player_name
    )

    for query_index, query in enumerate(
        queries
    ):

        if query_index == 0:
            search_pass = "primary"
        else:
            search_pass = "fallback"

        # ----------------------------------------------------
        # Avoid duplicate queries
        # ----------------------------------------------------

        query_key = query.lower().strip()

        if query_key in SEARCH_QUERY_CACHE:
            continue

        SEARCH_QUERY_CACHE.add(
            query_key
        )

        used_queries.append({
            "query": query,
            "pass": search_pass
        })

        results = search_wikidata(
            query=query,
            limit=SEARCH_LIMIT,
            pass_name=search_pass
        )

        for result in results:

            qid = result.get("id")

            if not qid:
                continue

            # ------------------------------------------------
            # QID deduplication
            # ------------------------------------------------

            if qid in all_candidates:
                continue

            entity = get_entity(qid)

            if not entity:
                continue

            candidate = build_candidate_from_entity(
                entity=entity,
                source_name=player_name,
                source_year=source_year
            )

            candidate["searchQuery"] = query
            candidate["searchPass"] = search_pass

            all_candidates[qid] = candidate

        # ----------------------------------------------------
        # Primary result check
        # ----------------------------------------------------

        candidate_list = list(
            all_candidates.values()
        )

        best, sorted_candidates = (
            select_best_match(
                candidate_list
            )
        )

        if best:

            classification = classify_identity(
                best
            )

            # A SAFE candidate is enough to stop.
            if classification == "SAFE":

                if search_pass == "primary":

                    SEARCH_DIAGNOSTICS[
                        "primary_matches"
                    ] += 1

                else:

                    SEARCH_DIAGNOSTICS[
                        "fallback_matches"
                    ] += 1

                SEARCH_DIAGNOSTICS[
                    "candidate_diagnostics"
                ].append({
                    "playerName": player_name,
                    "bestQid": best.get(
                        "wikidataId"
                    ),
                    "bestName": best.get(
                        "matchedName"
                    ),
                    "bestScore": best.get(
                        "identityScore"
                    ),
                    "classification": classification,
                    "searchQuery": query,
                    "searchPass": search_pass,
                    "candidateCount": len(
                        candidate_list
                    ),
                })

                return (
                    best,
                    candidate_list,
                    used_queries
                )

        # ----------------------------------------------------
        # If primary query produced a very strong identity,
        # don't needlessly run every fallback query.
        # ----------------------------------------------------

        if query_index == 0 and best:

            if (
                best["nameScore"] >= 94
                and best["sameYear"]
            ):

                SEARCH_DIAGNOSTICS[
                    "primary_matches"
                ] += 1

                return (
                    best,
                    candidate_list,
                    used_queries
                )

    # --------------------------------------------------------
    # Final candidate selection
    # --------------------------------------------------------

    candidate_list = list(
        all_candidates.values()
    )

    best, sorted_candidates = (
        select_best_match(
            candidate_list
        )
    )

    if best:

        classification = classify_identity(
            best
        )

        SEARCH_DIAGNOSTICS[
            "candidate_diagnostics"
        ].append({
            "playerName": player_name,
            "bestQid": best.get(
                "wikidataId"
            ),
            "bestName": best.get(
                "matchedName"
            ),
            "bestScore": best.get(
                "identityScore"
            ),
            "classification": classification,
            "searchQuery": best.get(
                "searchQuery"
            ),
            "searchPass": best.get(
                "searchPass"
            ),
            "candidateCount": len(
                candidate_list
            ),
        })

        if best.get(
            "searchPass"
        ) == "fallback":

            SEARCH_DIAGNOSTICS[
                "fallback_matches"
            ] += 1

    return (
        best,
        candidate_list,
        used_queries
    )


# ============================================================
# MATCH AUDIT
# ============================================================

def build_match_audit(
    player,
    candidate,
    identity,
    dob_result,
    queries
):

    return {
        "playerId": player.get(
            "playerId"
        ),
        "playerName": player.get(
            "playerName"
        ),
        "sourceDateOfBirth": player.get(
            "dateOfBirth"
        ),
        "sourceDatePrecision": (
            get_source_precision(
                player.get(
                    "dateOfBirth"
                )
            )
        ),

        "wikidataId": (
            candidate.get(
                "wikidataId"
            )
            if candidate
            else None
        ),

        "matchedName": (
            candidate.get(
                "matchedName"
            )
            if candidate
            else None
        ),

        "wikidataDateOfBirth": (
            candidate.get(
                "wikidataDob"
            )
            if candidate
            else None
        ),

        "wikidataDatePrecision": (
            candidate.get(
                "wikidataDobPrecision"
            )
            if candidate
            else None
        ),

        "nameScore": (
            candidate.get(
                "nameScore"
            )
            if candidate
            else None
        ),

        "identityScore": (
            candidate.get(
                "identityScore"
            )
            if candidate
            else None
        ),

        "footballRelated": (
            candidate.get(
                "footballRelated"
            )
            if candidate
            else None
        ),

        "sameBirthYear": (
            candidate.get(
                "sameYear"
            )
            if candidate
            else None
        ),

        "identityClassification": identity,

        "dobAction": dob_result.get(
            "action"
        ),

        "dobReason": dob_result.get(
            "reason"
        ),

        "searchQuery": (
            candidate.get(
                "searchQuery"
            )
            if candidate
            else None
        ),

        "searchPass": (
            candidate.get(
                "searchPass"
            )
            if candidate
            else None
        ),

        "searchQueriesUsed": queries,
    }


# ============================================================
# MAIN
# ============================================================

def main():

    ensure_output_dir()

    print()
    print("=" * 60)
    print(
        f"Fyucha Player Database Wikidata V{VERSION}"
    )
    print("=" * 60)
    print()

    players = load_json(
        INPUT_FILE
    )

    if not isinstance(players, list):

        raise ValueError(
            "players.json must contain a JSON list"
        )

    test_players = players[
        :TEST_LIMIT
    ]

    enriched_players = []

    matches = []
    corrections = []
    conflicts = []
    year_only = []
    errors = []
    deceased = []

    matched_count = 0
    not_matched_count = 0

    safe_count = 0
    review_count = 0
    reject_count = 0

    full_dob_count = 0
    month_precision_count = 0
    year_precision_count = 0

    primary_match_count = 0
    fallback_match_count = 0

    fallback_query_count = 0
    total_search_queries = 0

    print(
        f"Testing first {len(test_players)} players..."
    )
    print()

    for index, player in enumerate(
        test_players,
        start=1
    ):

        player_name = (
            player.get(
                "playerName"
            )
            or player.get(
                "name"
            )
            or ""
        )

        source_dob = (
            player.get(
                "dateOfBirth"
            )
            or player.get(
                "dob"
            )
        )

        source_year = extract_year(
            source_dob
        )

        print(
            f"[{index}/{len(test_players)}] "
            f"{player_name}"
        )

        if not player_name:

            errors.append({
                "player": player,
                "error": "Missing player name"
            })

            enriched_players.append(
                player
            )

            continue

        # ----------------------------------------------------
        # DISCOVERY
        # ----------------------------------------------------

        try:

            best, candidates, used_queries = (
                discover_candidates(
                    player_name,
                    source_year
                )
            )

        except Exception as exc:

            errors.append({
                "playerId": player.get(
                    "playerId"
                ),
                "playerName": player_name,
                "error": (
                    "Discovery exception: "
                    + str(exc)
                )
            })

            enriched_players.append(
                player
            )

            continue

        total_search_queries += len(
            used_queries
        )

        fallback_query_count += sum(
            1
            for item in used_queries
            if item["pass"] == "fallback"
        )

        if not best:

            not_matched_count += 1

            errors.append({
                "playerId": player.get(
                    "playerId"
                ),
                "playerName": player_name,
                "error": "No Wikidata match",
                "queries": used_queries
            })

            enriched_players.append(
                player
            )

            continue

        matched_count += 1

        # ----------------------------------------------------
        # IDENTITY
        # ----------------------------------------------------

        identity = classify_identity(
            best
        )

        if identity == "SAFE":
            safe_count += 1

        elif identity == "REVIEW":
            review_count += 1

        else:
            reject_count += 1

        # ----------------------------------------------------
        # DOB
        # ----------------------------------------------------

        dob_result = evaluate_dob(
            source_dob,
            best,
            identity
        )

        action = dob_result[
            "action"
        ]

        # ----------------------------------------------------
        # PRECISION COUNTS
        # ----------------------------------------------------

        source_precision = (
            get_source_precision(
                source_dob
            )
        )

        if source_precision == "day":
            full_dob_count += 1

        elif source_precision == "month":
            month_precision_count += 1

        elif source_precision == "year":
            year_precision_count += 1

        # ----------------------------------------------------
        # CORRECTION
        # ----------------------------------------------------

        if action == "CORRECT":

            corrections.append({
                "playerId": player.get(
                    "playerId"
                ),
                "playerName": player_name,
                "sourceDateOfBirth": source_dob,
                "correctedDateOfBirth": best.get(
                    "wikidataDob"
                ),
                "wikidataId": best.get(
                    "wikidataId"
                ),
                "matchedName": best.get(
                    "matchedName"
                ),
                "identityClassification": identity,
                "reason": dob_result[
                    "reason"
                ],
            })

        # ----------------------------------------------------
        # CONFLICT
        # ----------------------------------------------------

        if action == "CONFLICT":

            conflicts.append({
                "playerId": player.get(
                    "playerId"
                ),
                "playerName": player_name,
                "sourceDateOfBirth": source_dob,
                "wikidataDateOfBirth": best.get(
                    "wikidataDob"
                ),
                "wikidataId": best.get(
                    "wikidataId"
                ),
                "matchedName": best.get(
                    "matchedName"
                ),
                "identityClassification": identity,
                "reason": dob_result[
                    "reason"
                ],
            })

        # ----------------------------------------------------
        # YEAR ONLY
        # ----------------------------------------------------

        if (
            source_precision == "year"
        ):

            year_only.append({
                "playerId": player.get(
                    "playerId"
                ),
                "playerName": player_name,
                "sourceDateOfBirth": source_dob,
                "wikidataDateOfBirth": best.get(
                    "wikidataDob"
                ),
                "wikidataPrecision": best.get(
                    "wikidataDobPrecision"
                ),
                "wikidataId": best.get(
                    "wikidataId"
                ),
            })

        # ----------------------------------------------------
        # DECEASED
        # ----------------------------------------------------

        if best.get(
            "_entity"
        ):

            entity = best[
                "_entity"
            ]

            death_claims = (
                entity
                .get("claims", {})
                .get("P570", [])
            )

            if death_claims:

                deceased.append({
                    "playerId": player.get(
                        "playerId"
                    ),
                    "playerName": player_name,
                    "wikidataId": best.get(
                        "wikidataId"
                    ),
                    "deathDateAvailable": True
                })

        # ----------------------------------------------------
        # MATCH AUDIT
        # ----------------------------------------------------

        audit = build_match_audit(
            player,
            best,
            identity,
            dob_result,
            used_queries
        )

        matches.append(
            audit
        )

        # ----------------------------------------------------
        # ENRICHED PLAYER
        # ----------------------------------------------------

        enriched = dict(
            player
        )

        enriched[
            "wikidataId"
        ] = best.get(
            "wikidataId"
        )

        enriched[
            "wikidataMatchedName"
        ] = best.get(
            "matchedName"
        )

        enriched[
            "wikidataDateOfBirth"
        ] = best.get(
            "wikidataDob"
        )

        enriched[
            "wikidataDatePrecision"
        ] = best.get(
            "wikidataDobPrecision"
        )

        enriched[
            "wikidataIdentityClassification"
        ] = identity

        enriched[
            "wikidataNameScore"
        ] = best.get(
            "nameScore"
        )

        enriched[
            "wikidataIdentityScore"
        ] = best.get(
            "identityScore"
        )

        enriched[
            "wikidataSearchPass"
        ] = best.get(
            "searchPass"
        )

        enriched[
            "wikidataSearchQuery"
        ] = best.get(
            "searchQuery"
        )

        # IMPORTANT:
        # Only apply automatic DOB correction
        # under the conservative evaluate_dob()
        # rules.

        if action == "CORRECT":

            enriched[
                "dateOfBirth"
            ] = best.get(
                "wikidataDob"
            )

        enriched_players.append(
            enriched
        )

    # ========================================================
    # FINAL DISCOVERY COUNTS
    # ========================================================

    for item in matches:

        if item.get(
            "searchPass"
        ) == "primary":

            primary_match_count += 1

        elif item.get(
            "searchPass"
        ) == "fallback":

            fallback_match_count += 1

    # ========================================================
    # API DIAGNOSTICS
    # ========================================================

    api_output = {
        "version": VERSION,
        "apiDiagnostics": {
            **API_DIAGNOSTICS,

            "status_codes": dict(
                API_DIAGNOSTICS[
                    "status_codes"
                ]
            ),

            "error_types": dict(
                API_DIAGNOSTICS[
                    "error_types"
                ]
            ),

            "search_error_types": dict(
                API_DIAGNOSTICS[
                    "search_error_types"
                ]
            ),

            "entity_error_types": dict(
                API_DIAGNOSTICS[
                    "entity_error_types"
                ]
            ),

            "search_status_codes": dict(
                API_DIAGNOSTICS[
                    "search_status_codes"
                ]
            ),

            "entity_status_codes": dict(
                API_DIAGNOSTICS[
                    "entity_status_codes"
                ]
            ),
        }
    }

    # ========================================================
    # SEARCH DIAGNOSTICS
    # ========================================================

    search_output = {
        "version": VERSION,

        "totalSearchQueries": (
            SEARCH_DIAGNOSTICS[
                "total_queries"
            ]
        ),

        "uniqueSearchQueries": (
            SEARCH_DIAGNOSTICS[
                "unique_queries"
            ]
        ),

        "cachedSearchQueries": (
            SEARCH_DIAGNOSTICS[
                "cached_queries"
            ]
        ),

        "primarySearchQueries": (
            SEARCH_DIAGNOSTICS[
                "primary_queries"
            ]
        ),

        "fallbackSearchQueries": (
            SEARCH_DIAGNOSTICS[
                "fallback_queries"
            ]
        ),

        "primaryMatches": (
            primary_match_count
        ),

        "fallbackMatches": (
            fallback_match_count
        ),

        "primaryApiErrors": (
            SEARCH_DIAGNOSTICS[
                "primary_api_errors"
            ]
        ),

        "fallbackApiErrors": (
            SEARCH_DIAGNOSTICS[
                "fallback_api_errors"
            ]
        ),

        "queryErrors": (
            SEARCH_DIAGNOSTICS[
                "query_errors"
            ]
        ),

        "fallbackAttempts": (
            SEARCH_DIAGNOSTICS[
                "fallback_attempts"
            ]
        ),

        "candidateDiagnostics": (
            SEARCH_DIAGNOSTICS[
                "candidate_diagnostics"
            ]
        ),

        "scoreGapDiagnostics": (
            SEARCH_DIAGNOSTICS[
                "score_gap_diagnostics"
            ]
        ),
    }

    # ========================================================
    # WRITE OUTPUTS
    # ========================================================

    write_json(
        ENRICHED_FILE,
        enriched_players
    )

    write_json(
        MATCHES_FILE,
        matches
    )

    write_json(
        CORRECTIONS_FILE,
        corrections
    )

    write_json(
        CONFLICTS_FILE,
        conflicts
    )

    write_json(
        YEAR_ONLY_FILE,
        year_only
    )

    write_json(
        ERRORS_FILE,
        errors
    )

    write_json(
        API_DIAGNOSTICS_FILE,
        api_output
    )

    write_json(
        SEARCH_DIAGNOSTICS_FILE,
        search_output
    )

    write_json(
        DECEASED_FILE,
        deceased
    )

    # ========================================================
    # REPORT
    # ========================================================

    print()
    print("=" * 60)
    print(
        f"V{VERSION} TEST COMPLETE"
    )
    print("=" * 60)

    print(
        f"Players tested:        {len(test_players)}"
    )

    print(
        f"Matched:               {matched_count}"
    )

    print(
        f"Not matched:           {not_matched_count}"
    )

    print(
        f"Errors:                {len(errors)}"
    )

    print(
        f"API errors:            "
        f"{API_DIAGNOSTICS['errors']}"
    )

    print(
        f"SAFE matches:          {safe_count}"
    )

    print(
        f"REVIEW matches:        {review_count}"
    )

    print(
        f"REJECT matches:        {reject_count}"
    )

    print(
        f"Full DOB records:      {full_dob_count}"
    )

    print(
        f"Month precision:       {month_precision_count}"
    )

    print(
        f"Year precision:        {year_precision_count}"
    )

    print(
        f"DOB corrections:       {len(corrections)}"
    )

    print(
        f"DOB conflicts:         {len(conflicts)}"
    )

    print(
        f"Deceased records:      {len(deceased)}"
    )

    print()
    print("DISCOVERY:")
    print(
        f"Primary-search matches:   "
        f"{primary_match_count}"
    )

    print(
        f"Fallback-search matches:  "
        f"{fallback_match_count}"
    )

    print(
        f"Fallback queries used:    "
        f"{fallback_query_count}"
    )

    print(
        f"Total search queries:     "
        f"{total_search_queries}"
    )

    if len(test_players) > 0:

        print(
            f"Match rate:               "
            f"{matched_count / len(test_players) * 100:.1f}%"
        )

    print()
    print("API DIAGNOSTICS:")

    print(
        f"Total API requests:       "
        f"{API_DIAGNOSTICS['total_requests']}"
    )

    print(
        f"Successful requests:      "
        f"{API_DIAGNOSTICS['successful_requests']}"
    )

    print(
        f"Retries:                  "
        f"{API_DIAGNOSTICS['retries']}"
    )

    print(
        f"Transient errors:         "
        f"{API_DIAGNOSTICS['transient_errors']}"
    )

    print(
        f"Permanent errors:         "
        f"{API_DIAGNOSTICS['permanent_errors']}"
    )

    print(
        f"Rate-limit errors:        "
        f"{API_DIAGNOSTICS['rate_limit_errors']}"
    )

    print(
        f"Server errors:            "
        f"{API_DIAGNOSTICS['server_errors']}"
    )

    print(
        f"Client errors:            "
        f"{API_DIAGNOSTICS['client_errors']}"
    )

    print(
        f"Timeout errors:            "
        f"{API_DIAGNOSTICS['timeout_errors']}"
    )

    print(
        f"Connection errors:         "
        f"{API_DIAGNOSTICS['connection_errors']}"
    )

    print()
    print("HTTP STATUS CODES:")

    for status, count in sorted(
        API_DIAGNOSTICS[
            "status_codes"
        ].items()
    ):

        print(
            f"  HTTP {status}: {count}"
        )

    print()
    print("SEARCH CACHE:")

    print(
        f"Unique queries:            "
        f"{SEARCH_DIAGNOSTICS['unique_queries']}"
    )

    print(
        f"Cached queries:            "
        f"{SEARCH_DIAGNOSTICS['cached_queries']}"
    )

    print()
    print("OUTPUT FILES:")

    print(
        f"  {ENRICHED_FILE}"
    )

    print(
        f"  {MATCHES_FILE}"
    )

    print(
        f"  {CORRECTIONS_FILE}"
    )

    print(
        f"  {CONFLICTS_FILE}"
    )

    print(
        f"  {YEAR_ONLY_FILE}"
    )

    print(
        f"  {ERRORS_FILE}"
    )

    print(
        f"  {API_DIAGNOSTICS_FILE}"
    )

    print(
        f"  {SEARCH_DIAGNOSTICS_FILE}"
    )

    print(
        f"  {DECEASED_FILE}"
    )

    print()
    print("=" * 60)


# ============================================================
# RUN
# ============================================================

if __name__ == "__main__":
    main()
