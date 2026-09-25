import json
import re
import time
import unicodedata
from pathlib import Path

import requests


# ============================================================
# Fyucha Player Database - Wikidata V2.3.1
#
# Workflow:
#
#     DISCOVER -> SCORE -> CLASSIFY -> DIAGNOSE -> APPLY
#
# V2.3.1 PURPOSE:
#
#   V2.2.9 achieved:
#       91 matched
#        9 not matched
#        8 DOB conflicts
#
#   V2.3.1 DOES NOT loosen the SAFE rules.
#
#   Instead, it explains why the remaining cases fail.
#
# NEW DIAGNOSTICS:
#
#   1. Every unmatched player receives a detailed diagnosis.
#   2. Top Wikidata candidates are recorded.
#   3. Candidate rejection reasons are recorded.
#   4. Search queries attempted are recorded.
#   5. DOB conflicts receive a detailed conflict diagnosis.
#   6. Alternative candidates are retained for audit.
#   7. Diagnostic summary is written separately.
#
# IMPORTANT:
#
#   This version is the FINAL PRODUCTION / DIAGNOSTIC version.
#
#   It does not automatically relax identity thresholds.
#   It does not automatically resolve DOB conflicts.
#
# ============================================================


VERSION = "2.4"


# ============================================================
# FILES
# ============================================================

INPUT_FILE = Path(
    "output/players.json"
)

OUTPUT_DIR = Path(
    "output"
)

ENRICHED_FILE = (
    OUTPUT_DIR
    / "enriched-players.json"
)

MATCHES_FILE = (
    OUTPUT_DIR
    / "wikidata-matches.json"
)

CORRECTIONS_FILE = (
    OUTPUT_DIR
    / "dob-corrections.json"
)

CONFLICTS_FILE = (
    OUTPUT_DIR
    / "dob-conflicts.json"
)

YEAR_ONLY_FILE = (
    OUTPUT_DIR
    / "year-only.json"
)

ERRORS_FILE = (
    OUTPUT_DIR
    / "wikidata-errors.json"
)

# NEW V2.3.1 FILES

UNMATCHED_DIAGNOSTICS_FILE = (
    OUTPUT_DIR
    / "unmatched-diagnostics.json"
)

CONFLICT_DIAGNOSTICS_FILE = (
    OUTPUT_DIR
    / "conflict-diagnostics.json"
)

DIAGNOSTICS_SUMMARY_FILE = (
    OUTPUT_DIR
    / "diagnostics-summary.json"
)


# ============================================================
# SETTINGS
# ============================================================

TEST_LIMIT = None

# Maximum results requested from each Wikidata search.
SEARCH_LIMIT = 10

# Keep retries low so a temporary Wikidata problem cannot stall
# the entire GitHub Actions workflow for hours.
MAX_RETRIES = 2

BASE_DELAY = 1.0

# Small pause between entity requests. Search requests themselves
# are not artificially delayed.
REQUEST_DELAY = 0.05

# V2.4 performance control: normally load only the most promising
# search results instead of fetching every returned Wikidata entity.
ENTITY_LOAD_LIMIT = 3

# If a search result is an exact/near-exact name match, load it even
# when it falls outside the normal top-N entity window.
STRONG_NAME_SCORE = 94

# Number of candidates to preserve in diagnostics.
DIAGNOSTIC_TOP_CANDIDATES = 5


USER_AGENT = (
    "FyuchaPlayerDatabase/2.4 "
    "(football player birthday database)"
)


SEARCH_URL = (
    "https://www.wikidata.org/w/api.php"
)


ENTITY_URL = (
    "https://www.wikidata.org/wiki/"
    "Special:EntityData/{}.json"
)


# ============================================================
# SESSION
# ============================================================

session = requests.Session()

session.headers.update({
    "User-Agent": USER_AGENT,
    "Accept": "application/json"
})


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
        c
        for c in value
        if not unicodedata.combining(c)
    )

    value = value.lower()

    value = value.replace(
        "’",
        "'"
    )

    value = value.replace(
        "'",
        ""
    )

    value = value.replace(
        "-",
        " "
    )

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

    return normalize_name(
        value
    ).replace(
        " ",
        ""
    )


def name_tokens(value):

    value = normalize_name(
        value
    )

    if not value:
        return set()

    return set(
        value.split()
    )


# ============================================================
# SEARCH QUERY BUILDER
# ============================================================

def build_search_queries(name):

    if not name:
        return []

    normalized = normalize_name(
        name
    )

    if not normalized:
        return []

    queries = []

    def add_query(query):

        query = str(
            query
        ).strip()

        if not query:
            return

        normalized_query = normalize_name(
            query
        )

        if not normalized_query:
            return

        existing = {
            normalize_name(q)
            for q in queries
        }

        if normalized_query not in existing:

            queries.append(
                query
            )

    # --------------------------------------------------------
    # ORIGINAL
    # --------------------------------------------------------

    add_query(
        name
    )

    # --------------------------------------------------------
    # NORMALIZED
    # --------------------------------------------------------

    if str(name).strip() != normalized:

        add_query(
            normalized
        )

    # --------------------------------------------------------
    # FOOTBALL
    # --------------------------------------------------------

    add_query(
        f"{normalized} footballer"
    )

    add_query(
        f"{normalized} football player"
    )

    add_query(
        f"{normalized} soccer player"
    )

    # --------------------------------------------------------
    # FIRST NAME + SURNAME
    # --------------------------------------------------------

    tokens = normalized.split()

    if len(tokens) >= 3:

        first_name = tokens[0]

        surname = tokens[-1]

        if (
            first_name
            and surname
            and first_name != surname
        ):

            add_query(
                f"{first_name} {surname}"
            )

            add_query(
                f"{first_name} {surname} footballer"
            )

    return queries


# ============================================================
# DOB PRECISION
# ============================================================

def get_source_precision(player):

    explicit = player.get(
        "dateOfBirthPrecision"
    )

    if explicit in (
        "year",
        "month",
        "day"
    ):

        return explicit

    date = player.get(
        "dateOfBirth"
    )

    if not date:
        return None

    date = str(
        date
    )

    if re.fullmatch(
        r"\d{4}",
        date
    ):

        return "year"

    if re.fullmatch(
        r"\d{4}-\d{2}",
        date
    ):

        return "month"

    if re.fullmatch(
        r"\d{4}-\d{2}-\d{2}",
        date
    ):

        return "day"

    return None


def get_year(date):

    if not date:
        return None

    match = re.match(
        r"^(\d{4})",
        str(date)
    )

    if not match:
        return None

    return int(
        match.group(1)
    )


# ============================================================
# YEAR PLACEHOLDER
# ============================================================

def is_likely_year_placeholder(player):

    date = player.get(
        "dateOfBirth"
    )

    if not date:
        return False

    date = str(
        date
    )

    if not re.fullmatch(
        r"\d{4}-01-01",
        date
    ):

        return False

    precision = player.get(
        "dateOfBirthPrecision"
    )

    if precision == "year":
        return True

    if not precision:
        return True

    return False


def effective_source_precision(player):

    if is_likely_year_placeholder(
        player
    ):

        return "year"

    return get_source_precision(
        player
    )


# ============================================================
# HTTP
# ============================================================

def request_json(
    url,
    params=None
):

    last_error = None

    for attempt in range(
        1,
        MAX_RETRIES + 1
    ):

        try:

            response = session.get(
                url,
                params=params,
                timeout=30
            )

            if response.status_code == 429:

                wait = (
                    BASE_DELAY
                    * (2 ** (attempt - 1))
                )

                print(
                    "    Rate limited. "
                    f"Retrying in {wait:.1f}s..."
                )

                time.sleep(
                    wait
                )

                continue

            if response.status_code >= 500:

                wait = (
                    BASE_DELAY
                    * (2 ** (attempt - 1))
                )

                print(
                    "    Wikidata server error "
                    f"{response.status_code}. "
                    f"Retrying in {wait:.1f}s..."
                )

                time.sleep(
                    wait
                )

                continue

            response.raise_for_status()

            return response.json(), None

        except Exception as exc:

            last_error = str(
                exc
            )

            if attempt < MAX_RETRIES:

                wait = (
                    BASE_DELAY
                    * (2 ** (attempt - 1))
                )

                time.sleep(
                    wait
                )

    return None, last_error


# ============================================================
# WIKIDATA SEARCH
# ============================================================

def search_wikidata(query):

    if not query:
        return [], None

    params = {
        "action": "wbsearchentities",
        "search": query,
        "language": "en",
        "uselang": "en",
        "format": "json",
        "limit": SEARCH_LIMIT
    }

    data, error = request_json(
        SEARCH_URL,
        params
    )

    if error:
        return None, error

    if not isinstance(
        data,
        dict
    ):

        return [], None

    results = data.get(
        "search",
        []
    )

    if not isinstance(
        results,
        list
    ):

        return [], None

    return results, None


# ============================================================
# ENTITY
# ============================================================

def get_entity(qid):

    if not qid:
        return None, None

    data, error = request_json(
        ENTITY_URL.format(qid)
    )

    if error:
        return None, error

    if not isinstance(
        data,
        dict
    ):

        return None, None

    entities = data.get(
        "entities",
        {}
    )

    if not isinstance(
        entities,
        dict
    ):

        return None, None

    return (
        entities.get(qid),
        None
    )


# ============================================================
# ENTITY TEXT
# ============================================================

def entity_label(entity):

    if not isinstance(
        entity,
        dict
    ):

        return ""

    labels = entity.get(
        "labels",
        {}
    )

    if not isinstance(
        labels,
        dict
    ):

        return ""

    entry = labels.get(
        "en",
        {}
    )

    if not isinstance(
        entry,
        dict
    ):

        return ""

    return entry.get(
        "value",
        ""
    )


def entity_aliases(entity):

    if not isinstance(
        entity,
        dict
    ):

        return []

    aliases = entity.get(
        "aliases",
        {}
    )

    if not isinstance(
        aliases,
        dict
    ):

        return []

    result = []

    for item in aliases.get(
        "en",
        []
    ):

        if not isinstance(
            item,
            dict
        ):

            continue

        value = item.get(
            "value"
        )

        if value:

            result.append(
                value
            )

    return result


def entity_description(entity):

    if not isinstance(
        entity,
        dict
    ):

        return ""

    descriptions = entity.get(
        "descriptions",
        {}
    )

    if not isinstance(
        descriptions,
        dict
    ):

        return ""

    entry = descriptions.get(
        "en",
        {}
    )

    if not isinstance(
        entry,
        dict
    ):

        return ""

    return entry.get(
        "value",
        ""
    )


# ============================================================
# NAME SCORE
# ============================================================

def score_name(
    source,
    candidate
):

    source_normalized = normalize_name(
        source
    )

    candidate_normalized = normalize_name(
        candidate
    )

    if not source_normalized:
        return 0, "no-match"

    if not candidate_normalized:
        return 0, "no-match"

    if (
        source_normalized
        == candidate_normalized
    ):

        return 100, "exact-name"

    if (
        compact_name(source)
        == compact_name(candidate)
    ):

        return 96, "compact-name"

    source_tokens = name_tokens(
        source
    )

    candidate_tokens = name_tokens(
        candidate
    )

    if (
        source_tokens
        == candidate_tokens
    ):

        return 94, "same-token-set"

    common = (
        source_tokens
        & candidate_tokens
    )

    if common:

        ratio = (
            len(common)
            / max(
                len(source_tokens),
                len(candidate_tokens)
            )
        )

        if ratio >= 0.75:

            return (
                85,
                "strong-token-overlap"
            )

        if ratio >= 0.50:

            return (
                70,
                "partial-token-overlap"
            )

    return 0, "no-match"


# ============================================================
# DOB EXTRACTION
# ============================================================

def extract_dob(entity):

    claims = entity.get(
        "claims",
        {}
    )

    if not isinstance(
        claims,
        dict
    ):

        return {
            "date": None,
            "precision": None,
            "year": None,
            "rank": 0,
            "precisionNumber": None
        }

    dob_claims = claims.get(
        "P569",
        []
    )

    if not isinstance(
        dob_claims,
        list
    ):

        dob_claims = []

    best = None

    precision_rank = {
        "year": 1,
        "month": 2,
        "day": 3
    }

    for claim in dob_claims:

        if not isinstance(
            claim,
            dict
        ):

            continue

        mainsnak = claim.get(
            "mainsnak",
            {}
        )

        if not isinstance(
            mainsnak,
            dict
        ):

            continue

        datavalue = mainsnak.get(
            "datavalue"
        )

        if not isinstance(
            datavalue,
            dict
        ):

            continue

        value = datavalue.get(
            "value",
            {}
        )

        if not isinstance(
            value,
            dict
        ):

            continue

        raw_time = value.get(
            "time"
        )

        precision_number = value.get(
            "precision"
        )

        if not raw_time:
            continue

        raw_time = str(
            raw_time
        ).lstrip(
            "+"
        )

        match = re.match(
            r"(\d{4})-(\d{2})-(\d{2})",
            raw_time
        )

        if not match:
            continue

        year = match.group(1)
        month = match.group(2)
        day = match.group(3)

        try:

            precision_number = int(
                precision_number
            )

        except (
            TypeError,
            ValueError
        ):

            precision_number = None

        if precision_number == 11:

            if (
                month == "00"
                or day == "00"
            ):

                continue

            date = (
                f"{year}-{month}-{day}"
            )

            precision = "day"

        elif precision_number == 10:

            if month == "00":

                date = year
                precision = "year"

            else:

                date = (
                    f"{year}-{month}"
                )

                precision = "month"

        elif precision_number == 9:

            date = year
            precision = "year"

        else:

            if (
                month != "00"
                and day != "00"
            ):

                date = (
                    f"{year}-{month}-{day}"
                )

                precision = "review"

            elif month != "00":

                date = (
                    f"{year}-{month}"
                )

                precision = "review"

            else:

                date = year
                precision = "year"

        rank = precision_rank.get(
            precision,
            0
        )

        if (
            best is None
            or rank > best["rank"]
        ):

            best = {
                "date": date,
                "precision": precision,
                "year": int(year),
                "rank": rank,
                "precisionNumber": (
                    precision_number
                )
            }

    if best:
        return best

    return {
        "date": None,
        "precision": None,
        "year": None,
        "rank": 0,
        "precisionNumber": None
    }


# ============================================================
# DEATH DATE
# ============================================================

def extract_death_date(entity):

    claims = entity.get(
        "claims",
        {}
    )

    if not isinstance(
        claims,
        dict
    ):

        return None

    for claim in claims.get(
        "P570",
        []
    ):

        if not isinstance(
            claim,
            dict
        ):

            continue

        mainsnak = claim.get(
            "mainsnak",
            {}
        )

        if not isinstance(
            mainsnak,
            dict
        ):

            continue

        datavalue = mainsnak.get(
            "datavalue"
        )

        if not isinstance(
            datavalue,
            dict
        ):

            continue

        value = datavalue.get(
            "value",
            {}
        )

        if not isinstance(
            value,
            dict
        ):

            continue

        raw_time = value.get(
            "time"
        )

        if raw_time:

            return str(
                raw_time
            ).lstrip(
                "+"
            )

    return None


# ============================================================
# FOOTBALL RELEVANCE
# ============================================================

FOOTBALL_TERMS = (
    "footballer",
    "football player",
    "soccer player",
    "football goalkeeper",
    "football midfielder",
    "football defender",
    "football forward",
    "football striker",
    "football manager",
    "football coach",
    "soccer manager",
    "soccer coach"
)


def football_relevance(
    description
):

    text = normalize_name(
        description
    )

    for term in FOOTBALL_TERMS:

        if (
            normalize_name(term)
            in text
        ):

            return True

    return False


# ============================================================
# CANDIDATE BUILDER
# ============================================================

def build_candidate_from_entity(
    player,
    entity
):

    if not isinstance(
        entity,
        dict
    ):

        return None, None

    qid = entity.get(
        "id"
    )

    if not qid:
        return None, None

    source_name = (
        player.get("displayName")
        or player.get("name")
        or ""
    )

    label = entity_label(
        entity
    )

    aliases = entity_aliases(
        entity
    )

    description = entity_description(
        entity
    )

    # --------------------------------------------------------
    # NAME
    # --------------------------------------------------------

    name_score, name_method = (
        score_name(
            source_name,
            label
        )
    )

    matched_alias = None

    for alias in aliases:

        alias_score, alias_method = (
            score_name(
                source_name,
                alias
            )
        )

        if alias_score > name_score:

            name_score = alias_score

            name_method = (
                f"alias-{alias_method}"
            )

            matched_alias = alias

    # --------------------------------------------------------
    # DOB
    # --------------------------------------------------------

    dob = extract_dob(
        entity
    )

    source_year = get_year(
        player.get(
            "dateOfBirth"
        )
    )

    same_year = (
        source_year is not None
        and dob["year"] is not None
        and source_year == dob["year"]
    )

    different_year = (
        source_year is not None
        and dob["year"] is not None
        and source_year != dob["year"]
    )

    football = football_relevance(
        description
    )

    # --------------------------------------------------------
    # IDENTITY SCORE
    # --------------------------------------------------------

    identity_score = name_score

    if football:

        identity_score += 15

    if same_year:

        identity_score += 10

    if different_year:

        identity_score -= 5

    return {
        "qid": qid,
        "label": label,
        "aliases": aliases,
        "matchedAlias": matched_alias,
        "description": description,
        "nameScore": name_score,
        "nameMatchMethod": name_method,
        "footballRelated": football,
        "dob": dob,
        "sourceYear": source_year,
        "sameYear": same_year,
        "differentYear": different_year,
        "identityScore": identity_score,
        "deathDate": extract_death_date(
            entity
        )
    }, None


# ============================================================
# CANDIDATE DIAGNOSTIC REASON
# ============================================================

def candidate_failure_reasons(
    player,
    candidate
):

    reasons = []

    if not candidate:

        reasons.append(
            "no-candidate"
        )

        return reasons

    name_score = candidate.get(
        "nameScore",
        0
    )

    football = candidate.get(
        "footballRelated",
        False
    )

    same_year = candidate.get(
        "sameYear",
        False
    )

    different_year = candidate.get(
        "differentYear",
        False
    )

    dob = candidate.get(
        "dob",
        {}
    )

    wikidata_precision = dob.get(
        "precision"
    )

    source_precision = effective_source_precision(
        player
    )

    if name_score < 65:

        reasons.append(
            "name-score-below-65"
        )

    elif name_score < 78:

        reasons.append(
            "name-score-between-65-and-77"
        )

    elif name_score < 94:

        reasons.append(
            "name-score-between-78-and-93"
        )

    if not football:

        reasons.append(
            "football-relevance-not-confirmed"
        )

    if different_year:

        reasons.append(
            "birth-year-conflict"
        )

    if not same_year:

        reasons.append(
            "birth-year-not-confirmed"
        )

    if not dob.get("date"):

        reasons.append(
            "wikidata-dob-unavailable"
        )

    elif wikidata_precision == "review":

        reasons.append(
            "wikidata-dob-precision-uncertain"
        )

    if source_precision == "year":

        if wikidata_precision == "year":

            reasons.append(
                "both-source-and-wikidata-are-year-only"
            )

        elif wikidata_precision == "month":

            if same_year:

                reasons.append(
                    "month-precision-available"
                )

        elif wikidata_precision == "day":

            if same_year:

                reasons.append(
                    "full-day-precision-available"
                )

    if source_precision == "day":

        if (
            dob.get("date")
            and player.get("dateOfBirth")
            != dob.get("date")
            and same_year
        ):

            reasons.append(
                "same-year-full-dob-disagreement"
            )

    return reasons


# ============================================================
# IDENTITY CLASSIFICATION
# ============================================================

def classify_identity(
    candidate
):

    if not candidate:

        return (
            "REJECT",
            "no-candidate"
        )

    name_score = candidate.get(
        "nameScore",
        0
    )

    football = candidate.get(
        "footballRelated",
        False
    )

    same_year = candidate.get(
        "sameYear",
        False
    )

    different_year = candidate.get(
        "differentYear",
        False
    )

    if name_score < 65:

        return (
            "REJECT",
            "weak-name-match"
        )

    if name_score >= 94:

        if football:

            return (
                "SAFE",
                "strong-name-football-identity"
            )

        if same_year:

            return (
                "REVIEW",
                "strong-name-but-football-relevance-unconfirmed"
            )

        if different_year:

            return (
                "REVIEW",
                "strong-name-but-year-conflict"
            )

        return (
            "REVIEW",
            "strong-name-insufficient-football-evidence"
        )

    if (
        name_score >= 78
        and football
    ):

        if same_year:

            return (
                "SAFE",
                "strong-name-football-same-year"
            )

        if different_year:

            return (
                "REVIEW",
                "strong-name-football-year-conflict"
            )

        return (
            "REVIEW",
            "strong-name-football-year-unconfirmed"
        )

    if (
        name_score >= 65
        and same_year
    ):

        return (
            "REVIEW",
            "partial-name-same-year"
        )

    return (
        "REVIEW",
        "identity-needs-review"
    )


# ============================================================
# DOB DECISION
# ============================================================

def evaluate_dob(
    player,
    candidate
):

    source_date = player.get(
        "dateOfBirth"
    )

    source_precision = (
        effective_source_precision(
            player
        )
    )

    source_year = get_year(
        source_date
    )

    wikidata_date = (
        candidate.get("dob", {})
        .get("date")
    )

    wikidata_precision = (
        candidate.get("dob", {})
        .get("precision")
    )

    wikidata_year = (
        candidate.get("dob", {})
        .get("year")
    )

    identity_classification, identity_reason = (
        classify_identity(
            candidate
        )
    )

    base = {
        "classification": (
            identity_classification
        ),
        "identityReason": (
            identity_reason
        ),
        "action": "keep-source",
        "method": "no-change"
    }

    # --------------------------------------------------------
    # NO WIKIDATA DOB
    # --------------------------------------------------------

    if not wikidata_date:

        base["method"] = (
            "wikidata-dob-unavailable"
        )

        return base

    # --------------------------------------------------------
    # YEAR CONFLICT
    # --------------------------------------------------------

    if (
        source_year is not None
        and wikidata_year is not None
        and source_year != wikidata_year
    ):

        if (
            candidate.get("nameScore", 0)
            >= 94
            and candidate.get(
                "footballRelated",
                False
            )
        ):

            base["classification"] = (
                "REJECT"
            )

            base["identityReason"] = (
                "strong-identity-year-conflict"
            )

        else:

            base["classification"] = (
                "REVIEW"
            )

            base["identityReason"] = (
                "possible-identity-year-conflict"
            )

        base["action"] = (
            "conflict"
        )

        base["method"] = (
            "year-conflict"
        )

        return base

    # --------------------------------------------------------
    # SOURCE YEAR
    # --------------------------------------------------------

    if source_precision == "year":

        if (
            identity_classification
            != "SAFE"
        ):

            if wikidata_year == source_year:

                base["method"] = (
                    "same-year-review"
                )

            else:

                base["method"] = (
                    "year-only"
                )

            return base

        if (
            wikidata_year == source_year
            and wikidata_precision == "day"
        ):

            if wikidata_date != source_date:

                base["action"] = (
                    "correct"
                )

                base["method"] = (
                    "year-to-full-dob"
                )

                return base

            base["method"] = (
                "same-date"
            )

            return base

        if (
            wikidata_year == source_year
            and wikidata_precision == "month"
        ):

            base["action"] = (
                "correct-month"
            )

            base["method"] = (
                "year-to-month"
            )

            return base

        base["method"] = (
            "year-only"
        )

        return base

    # --------------------------------------------------------
    # SOURCE MONTH
    # --------------------------------------------------------

    if source_precision == "month":

        source_month = str(
            source_date
        )[:7]

        if (
            wikidata_precision == "day"
            and wikidata_date.startswith(
                source_month
            )
        ):

            if wikidata_date != source_date:

                base["action"] = (
                    "correct"
                )

                base["method"] = (
                    "month-to-full-dob"
                )

                return base

        if (
            wikidata_precision == "month"
            and wikidata_date == source_month
        ):

            base["method"] = (
                "month-match"
            )

            return base

        base["method"] = (
            "month-review"
        )

        return base

    # --------------------------------------------------------
    # SOURCE DAY
    # --------------------------------------------------------

    if source_precision == "day":

        if source_date == wikidata_date:

            base["method"] = (
                "full-date-match"
            )

            return base

        if (
            source_year is not None
            and wikidata_year is not None
            and source_year == wikidata_year
        ):

            base["action"] = (
                "dob-disagreement"
            )

            base["method"] = (
                "same-year-dob-disagreement"
            )

            if identity_classification == "SAFE":

                base["classification"] = (
                    "REVIEW"
                )

            return base

        base["action"] = (
            "conflict"
        )

        base["method"] = (
            "year-conflict"
        )

        base["classification"] = (
            "REJECT"
        )

        return base

    # --------------------------------------------------------
    # UNKNOWN
    # --------------------------------------------------------

    base["classification"] = (
        "REVIEW"
    )

    base["identityReason"] = (
        "source-dob-precision-unknown"
    )

    base["method"] = (
        "precision-unknown"
    )

    return base


# ============================================================
# CANDIDATE SORTING
# ============================================================

def sort_candidates(
    candidates
):

    valid = [
        c
        for c in candidates
        if isinstance(c, dict)
    ]

    valid.sort(
        key=lambda c: (
            c.get(
                "identityScore",
                0
            ),
            c.get(
                "nameScore",
                0
            ),
            c.get(
                "sameYear",
                False
            ),
            c.get(
                "footballRelated",
                False
            )
        ),
        reverse=True
    )

    return valid


# ============================================================
# SELECT BEST MATCH
# ============================================================

def select_best_match(
    candidates
):

    valid = sort_candidates(
        candidates
    )

    if not valid:
        return None

    best = valid[0]

    name_score = best.get(
        "nameScore",
        0
    )

    football = best.get(
        "footballRelated",
        False
    )

    same_year = best.get(
        "sameYear",
        False
    )

    if name_score >= 94:

        return best

    if (
        name_score >= 78
        and football
    ):

        return best

    if (
        name_score >= 65
        and same_year
    ):

        return best

    return None


# ============================================================
# DIAGNOSTIC CANDIDATE RECORD
# ============================================================

def diagnostic_candidate(
    player,
    candidate
):

    classification, reason = (
        classify_identity(
            candidate
        )
    )

    return {

        "wikidataId": candidate.get(
            "qid"
        ),

        "matchedName": candidate.get(
            "label"
        ),

        "matchedAlias": candidate.get(
            "matchedAlias"
        ),

        "description": candidate.get(
            "description"
        ),

        "nameScore": candidate.get(
            "nameScore"
        ),

        "nameMatchMethod": candidate.get(
            "nameMatchMethod"
        ),

        "identityScore": candidate.get(
            "identityScore"
        ),

        "footballRelated": candidate.get(
            "footballRelated"
        ),

        "sourceBirthYear": candidate.get(
            "sourceYear"
        ),

        "wikidataBirthYear": (
            candidate.get(
                "dob",
                {}
            ).get(
                "year"
            )
        ),

        "sourceDateOfBirth": player.get(
            "dateOfBirth"
        ),

        "wikidataDateOfBirth": (
            candidate.get(
                "dob",
                {}
            ).get(
                "date"
            )
        ),

        "wikidataDatePrecision": (
            candidate.get(
                "dob",
                {}
            ).get(
                "precision"
            )
        ),

        "sameYear": candidate.get(
            "sameYear"
        ),

        "differentYear": candidate.get(
            "differentYear"
        ),

        "classification": classification,

        "classificationReason": reason,

        "failureReasons": (
            candidate_failure_reasons(
                player,
                candidate
            )
        ),

        "deathDate": candidate.get(
            "deathDate"
        ),

        "searchQuery": candidate.get(
            "searchQuery"
        ),

        "searchPass": candidate.get(
            "searchPass"
        )
    }


# ============================================================
# UNMATCHED DIAGNOSIS
# ============================================================

def diagnose_unmatched(
    player,
    name,
    candidates,
    search_queries,
    search_errors,
    search_stage_details=None,
    search_stop_reason=None
):

    ranked = sort_candidates(
        candidates
    )

    top = ranked[
        :DIAGNOSTIC_TOP_CANDIDATES
    ]

    if not ranked:

        if search_errors and not search_queries:

            primary_reason = (
                "search-stage-error"
            )

        elif search_errors and len(search_errors) >= len(search_queries):

            primary_reason = (
                "search-stage-errors"
            )

        elif search_queries:

            primary_reason = (
                "no-wikidata-candidates-found"
            )

        else:

            primary_reason = (
                "no-search-query-generated"
            )

    else:

        best = ranked[0]

        reasons = candidate_failure_reasons(
            player,
            best
        )

        if reasons:

            primary_reason = (
                reasons[0]
            )

        else:

            primary_reason = (
                "candidate-found-but-selection-failed"
            )

    return {

        "id": player.get(
            "id"
        ),

        "name": name,

        "sourceDateOfBirth": player.get(
            "dateOfBirth"
        ),

        "sourceDatePrecision": (
            effective_source_precision(
                player
            )
        ),

        "sourceBirthYear": get_year(
            player.get(
                "dateOfBirth"
            )
        ),

        "primaryFailureReason": (
            primary_reason
        ),

        "candidateCount": len(
            candidates
        ),

        "searchQueries": search_queries,

        "searchQueriesAttempted": len(
            search_queries
        ),

        "searchErrors": search_errors,

        "searchStages": search_stage_details or [],

        "searchStopReason": search_stop_reason,

        "topCandidates": [
            diagnostic_candidate(
                player,
                c
            )
            for c in top
        ]
    }


# ============================================================
# CONFLICT DIAGNOSIS
# ============================================================

def diagnose_conflict(
    player,
    name,
    match,
    dob_result,
    candidates
):

    source_date = player.get(
        "dateOfBirth"
    )

    source_year = get_year(
        source_date
    )

    wikidata_date = (
        match.get(
            "dob",
            {}
        ).get(
            "date"
        )
    )

    wikidata_year = (
        match.get(
            "dob",
            {}
        ).get(
            "year"
        )
    )

    wikidata_precision = (
        match.get(
            "dob",
            {}
        ).get(
            "precision"
        )
    )

    if (
        source_year is not None
        and wikidata_year is not None
        and source_year != wikidata_year
    ):

        conflict_type = (
            "birth-year-conflict"
        )

    elif (
        source_date
        and wikidata_date
        and source_date != wikidata_date
    ):

        conflict_type = (
            "same-year-dob-disagreement"
        )

    else:

        conflict_type = (
            "other-dob-conflict"
        )

    ranked = sort_candidates(
        candidates
    )

    alternative_candidates = []

    for candidate in ranked:

        if (
            candidate.get("qid")
            == match.get("qid")
        ):

            continue

        alternative_candidates.append(
            diagnostic_candidate(
                player,
                candidate
            )
        )

        if len(
            alternative_candidates
        ) >= DIAGNOSTIC_TOP_CANDIDATES:

            break

    return {

        "id": player.get(
            "id"
        ),

        "name": name,

        "conflictType": conflict_type,

        "sourceDateOfBirth": source_date,

        "sourceDatePrecision": (
            effective_source_precision(
                player
            )
        ),

        "sourceBirthYear": source_year,

        "wikidataId": match.get(
            "qid"
        ),

        "matchedName": match.get(
            "label"
        ),

        "matchedAlias": match.get(
            "matchedAlias"
        ),

        "nameScore": match.get(
            "nameScore"
        ),

        "identityScore": match.get(
            "identityScore"
        ),

        "nameMatchMethod": match.get(
            "nameMatchMethod"
        ),

        "footballRelated": match.get(
            "footballRelated"
        ),

        "wikidataDateOfBirth": (
            wikidata_date
        ),

        "wikidataDatePrecision": (
            wikidata_precision
        ),

        "wikidataBirthYear": (
            wikidata_year
        ),

        "sameYear": match.get(
            "sameYear"
        ),

        "differentYear": match.get(
            "differentYear"
        ),

        "classification": (
            dob_result.get(
                "classification"
            )
        ),

        "identityReason": (
            dob_result.get(
                "identityReason"
            )
        ),

        "action": dob_result.get(
            "action"
        ),

        "method": dob_result.get(
            "method"
        ),

        "diagnosticExplanation": (
            candidate_failure_reasons(
                player,
                match
            )
        ),

        "searchQuery": match.get(
            "searchQuery"
        ),

        "searchPass": match.get(
            "searchPass"
        ),

        "alternativeCandidates": (
            alternative_candidates
        )
    }


# ============================================================
# MATCH AUDIT
# ============================================================

def build_match_audit(
    player,
    name,
    match,
    result,
    dob_result,
    source_precision
):

    return {

        "playerId": player.get(
            "id"
        ),

        "playerName": name,

        "sourceDateOfBirth": player.get(
            "dateOfBirth"
        ),

        "sourceDatePrecision": (
            source_precision
        ),

        "wikidataId": match.get(
            "qid"
        ),

        "matchedName": match.get(
            "label"
        ),

        "matchedAlias": match.get(
            "matchedAlias"
        ),

        "wikidataDateOfBirth": (
            match.get(
                "dob",
                {}
            ).get(
                "date"
            )
        ),

        "wikidataDatePrecision": (
            match.get(
                "dob",
                {}
            ).get(
                "precision"
            )
        ),

        "finalDateOfBirth": result.get(
            "dateOfBirth"
        ),

        "finalDatePrecision": result.get(
            "dateOfBirthPrecision"
        ),

        "matchScore": match.get(
            "identityScore"
        ),

        "nameScore": match.get(
            "nameScore"
        ),

        "nameMatchMethod": match.get(
            "nameMatchMethod"
        ),

        "matchMethod": dob_result.get(
            "method"
        ),

        "action": dob_result.get(
            "action"
        ),

        "classification": dob_result.get(
            "classification"
        ),

        "identityReason": dob_result.get(
            "identityReason"
        ),

        "footballRelated": match.get(
            "footballRelated"
        ),

        "sameYear": match.get(
            "sameYear"
        ),

        "differentYear": match.get(
            "differentYear"
        ),

        "searchQuery": match.get(
            "searchQuery"
        ),

        "searchPass": match.get(
            "searchPass"
        )
    }


# ============================================================
# MAIN
# ============================================================

def main():

    print()

    print(
        "=" * 60
    )

    print(
        f"Fyucha Player Database - Wikidata V{VERSION}"
    )

    print(
        "=" * 60
    )

    print()

    if not INPUT_FILE.exists():

        raise FileNotFoundError(
            f"Missing input file: {INPUT_FILE}"
        )

    with open(
        INPUT_FILE,
        "r",
        encoding="utf-8"
    ) as f:

        players = json.load(
            f
        )

    if not isinstance(
        players,
        list
    ):

        raise ValueError(
            "output/players.json must contain a JSON array."
        )

    print(
        f"Loaded {len(players):,} total players."
    )

    if TEST_LIMIT is None:
        test_players = players
        print(
            f"Processing all {len(test_players):,} players."
        )
    else:
        test_players = players[:TEST_LIMIT]
        print(
            f"Processing first {len(test_players):,} players (TEST_LIMIT={TEST_LIMIT})."
        )

    print()

    OUTPUT_DIR.mkdir(
        parents=True,
        exist_ok=True
    )

    # --------------------------------------------------------
    # OUTPUT COLLECTIONS
    # --------------------------------------------------------

    enriched = []

    matches = []

    corrections = []

    conflicts = []

    year_only = []

    api_errors = []

    unmatched_diagnostics = []

    conflict_diagnostics = []

    # --------------------------------------------------------
    # COUNTERS
    # --------------------------------------------------------

    matched = 0

    not_matched = 0

    errors = 0

    full_dob_records = 0

    month_precision_records = 0

    year_precision_records = 0

    corrected_dates = 0

    conflict_records = 0

    deceased_records = 0

    safe_records = 0

    review_records = 0

    reject_records = 0

    api_error_count = 0

    # --------------------------------------------------------
    # DISCOVERY
    # --------------------------------------------------------

    primary_search_matches = 0

    fallback_search_matches = 0

    fallback_queries_used = 0

    total_search_queries = 0

    primary_queries_attempted = 0

    fallback_queries_attempted = 0

    queries_with_results = 0

    queries_without_results = 0

    query_errors = 0

    search_candidates_discovered = 0

    search_candidates_rejected_duplicate = 0

    search_entities_loaded = 0

    search_entities_missing = 0

    search_candidates_built = 0

    search_early_stops = 0

    # --------------------------------------------------------
    # API DIAGNOSTICS
    # --------------------------------------------------------

    total_api_requests = 0

    successful_requests = 0

    retry_count = 0

    transient_errors = 0

    rate_limit_errors = 0

    server_errors = 0

    client_errors = 0

    timeout_errors = 0

    connection_errors = 0

    http_status_codes = {}

    # --------------------------------------------------------
    # CACHE
    # --------------------------------------------------------

    search_cache = {}

    entity_cache = {}

    search_cache_hits = 0

    search_cache_misses = 0

    entity_cache_hits = 0

    entity_cache_misses = 0

    # --------------------------------------------------------
    # WRAP REQUEST FOR METRICS
    # --------------------------------------------------------

    def tracked_request(
        url,
        params=None
    ):

        nonlocal total_api_requests
        nonlocal successful_requests
        nonlocal retry_count
        nonlocal transient_errors
        nonlocal rate_limit_errors
        nonlocal server_errors
        nonlocal client_errors
        nonlocal timeout_errors
        nonlocal connection_errors

        last_error = None

        for attempt in range(
            1,
            MAX_RETRIES + 1
        ):

            total_api_requests += 1

            try:

                response = session.get(
                    url,
                    params=params,
                    timeout=30
                )

                status = response.status_code

                http_status_codes[
                    str(status)
                ] = (
                    http_status_codes.get(
                        str(status),
                        0
                    ) + 1
                )

                if status == 429:

                    rate_limit_errors += 1

                    transient_errors += 1

                    if attempt < MAX_RETRIES:

                        retry_count += 1

                        wait = (
                            BASE_DELAY
                            * (2 ** (attempt - 1))
                        )

                        time.sleep(
                            wait
                        )

                        continue

                    last_error = (
                        "HTTP 429 rate limit"
                    )

                    break

                if status >= 500:

                    server_errors += 1

                    transient_errors += 1

                    if attempt < MAX_RETRIES:

                        retry_count += 1

                        wait = (
                            BASE_DELAY
                            * (2 ** (attempt - 1))
                        )

                        time.sleep(
                            wait
                        )

                        continue

                    last_error = (
                        f"HTTP {status}"
                    )

                    break

                if 400 <= status < 500:

                    client_errors += 1

                    last_error = (
                        f"HTTP {status}"
                    )

                    break

                response.raise_for_status()

                successful_requests += 1

                return response.json(), None

            except requests.Timeout as exc:

                timeout_errors += 1

                transient_errors += 1

                last_error = str(
                    exc
                )

                if attempt < MAX_RETRIES:

                    retry_count += 1

                    wait = (
                        BASE_DELAY
                        * (2 ** (attempt - 1))
                    )

                    time.sleep(
                        wait
                    )

                    continue

            except requests.ConnectionError as exc:

                connection_errors += 1

                transient_errors += 1

                last_error = str(
                    exc
                )

                if attempt < MAX_RETRIES:

                    retry_count += 1

                    wait = (
                        BASE_DELAY
                        * (2 ** (attempt - 1))
                    )

                    time.sleep(
                        wait
                    )

                    continue

            except Exception as exc:

                last_error = str(
                    exc
                )

                if attempt < MAX_RETRIES:

                    retry_count += 1

                    wait = (
                        BASE_DELAY
                        * (2 ** (attempt - 1))
                    )

                    time.sleep(
                        wait
                    )

                    continue

        return None, last_error

    # --------------------------------------------------------
    # CACHED SEARCH
    # --------------------------------------------------------

    def cached_search(
        query
    ):

        nonlocal search_cache_hits
        nonlocal search_cache_misses

        normalized_query = normalize_name(
            query
        )

        if (
            normalized_query
            in search_cache
        ):

            search_cache_hits += 1

            return search_cache[
                normalized_query
            ]

        search_cache_misses += 1

        result = tracked_request(
            SEARCH_URL,
            {
                "action": "wbsearchentities",
                "search": query,
                "language": "en",
                "uselang": "en",
                "format": "json",
                "limit": SEARCH_LIMIT
            }
        )

        data, error = result

        if error:

            search_cache[
                normalized_query
            ] = (
                None,
                error
            )

            return (
                None,
                error
            )

        if not isinstance(
            data,
            dict
        ):

            data = {}

        results = data.get(
            "search",
            []
        )

        if not isinstance(
            results,
            list
        ):

            results = []

        search_cache[
            normalized_query
        ] = (
            results,
            None
        )

        return (
            results,
            None
        )

    # --------------------------------------------------------
    # FAST SEARCH-RESULT RANKING
    # --------------------------------------------------------

    def quick_search_result_score(
        player_name,
        search_result
    ):

        if not isinstance(search_result, dict):
            return 0

        label = search_result.get("label") or ""
        aliases = search_result.get("aliases") or []
        description = (
            search_result.get("description") or ""
        ).lower()

        best_name_score = score_name(
            player_name,
            label
        )[0]

        if isinstance(aliases, list):
            for alias in aliases:
                if isinstance(alias, dict):
                    alias = alias.get("value") or ""
                if alias:
                    best_name_score = max(
                        best_name_score,
                        score_name(player_name, alias)[0]
                    )

        football_bonus = 0

        football_terms = (
            "football",
            "soccer",
            "footballer",
            "association football",
            "men's football",
            "women's football"
        )

        if any(
            term in description
            for term in football_terms
        ):
            football_bonus = 25

        return best_name_score + football_bonus

    def prioritize_search_results(
        player_name,
        search_results
    ):

        ranked = []

        for position, item in enumerate(
            search_results or []
        ):
            if not isinstance(item, dict):
                continue

            ranked.append((
                quick_search_result_score(
                    player_name,
                    item
                ),
                position,
                item
            ))

        ranked.sort(
            key=lambda row: (
                row[0],
                -row[1]
            ),
            reverse=True
        )

        selected = []
        selected_qids = set()

        # Always include strong name matches first.
        for quick_score, _, item in ranked:
            qid = item.get("id")
            if not qid or qid in selected_qids:
                continue

            name_score = quick_search_result_score(
                player_name,
                {
                    "label": item.get("label", ""),
                    "aliases": item.get("aliases", [])
                }
            )

            # quick score without football bonus is not directly
            # available above, so score label/aliases explicitly.
            best_name = score_name(
                player_name,
                item.get("label", "")
            )[0]

            for alias in item.get("aliases", []) or []:
                if isinstance(alias, dict):
                    alias = alias.get("value") or ""
                if alias:
                    best_name = max(
                        best_name,
                        score_name(player_name, alias)[0]
                    )

            if best_name >= STRONG_NAME_SCORE:
                selected.append(item)
                selected_qids.add(qid)

        # Fill the remainder with the best search results.
        for _, _, item in ranked:
            qid = item.get("id")
            if not qid or qid in selected_qids:
                continue
            if len(selected) >= ENTITY_LOAD_LIMIT:
                break
            selected.append(item)
            selected_qids.add(qid)

        return selected, ranked

    # --------------------------------------------------------
    # CACHED ENTITY
    # --------------------------------------------------------

    def cached_entity(
        qid
    ):

        nonlocal entity_cache_hits
        nonlocal entity_cache_misses

        if qid in entity_cache:

            entity_cache_hits += 1

            return (
                entity_cache[qid],
                None
            )

        entity_cache_misses += 1

        data, error = tracked_request(
            ENTITY_URL.format(qid)
        )

        if error:

            return (
                None,
                error
            )

        if not isinstance(
            data,
            dict
        ):

            return (
                None,
                None
            )

        entities = data.get(
            "entities",
            {}
        )

        if not isinstance(
            entities,
            dict
        ):

            return (
                None,
                None
            )

        entity = entities.get(
            qid
        )

        if entity is not None:

            entity_cache[
                qid
            ] = entity

        return (
            entity,
            None
        )

    # ========================================================
    # PROCESS
    # ========================================================

    for index, player in enumerate(
        test_players,
        start=1
    ):

        name = (
            player.get("displayName")
            or player.get("name")
            or ""
        )

        print(
            f"[{index}/{len(test_players)}] {name}"
        )

        original = dict(
            player
        )

        try:

            # ------------------------------------------------
            # BUILD SEARCH QUERIES
            # ------------------------------------------------

            search_queries = (
                build_search_queries(
                    name
                )
            )

            candidates = []

            candidate_qids = set()

            search_errors = []

            search_stage_details = []

            search_stop_reason = "all-search-stages-exhausted"

            # ------------------------------------------------
            # SEARCH
            # ------------------------------------------------

            for query_index, query in enumerate(
                search_queries,
                start=1
            ):

                is_primary = (
                    query_index == 1
                )

                if not is_primary:

                    fallback_queries_used += 1

                    fallback_queries_attempted += 1

                else:

                    primary_queries_attempted += 1

                total_search_queries += 1

                stage_detail = {
                    "queryIndex": query_index,
                    "query": query,
                    "searchPass": (
                        "primary"
                        if is_primary
                        else "fallback"
                    ),
                    "resultCount": 0,
                    "uniqueQids": 0,
                    "entitiesLoaded": 0,
                    "entitiesMissing": 0,
                    "candidatesBuilt": 0,
                    "duplicateQidsSkipped": 0,
                    "error": None,
                    "stopReason": None
                }

                search_stage_details.append(
                    stage_detail
                )

                search_results, search_error = (
                    cached_search(
                        query
                    )
                )

                if search_error:

                    api_error_count += 1

                    query_errors += 1

                    stage_detail["error"] = search_error

                    search_errors.append({
                        "query": query,
                        "error": search_error
                    })

                    api_errors.append({

                        "playerId": player.get(
                            "id"
                        ),

                        "playerName": name,

                        "stage": "search",

                        "searchQuery": query,

                        "searchPass": (
                            "primary"
                            if is_primary
                            else "fallback"
                        ),

                        "error": search_error
                    })

                    continue

                stage_detail["resultCount"] = len(
                    search_results or []
                )

                if stage_detail["resultCount"]:

                    queries_with_results += 1

                else:

                    queries_without_results += 1

                prioritized_results, ranked_results = (
                    prioritize_search_results(
                        name,
                        search_results
                    )
                )

                stage_detail["entityLoadLimit"] = ENTITY_LOAD_LIMIT
                stage_detail["searchResultsRanked"] = len(
                    ranked_results
                )
                stage_detail["selectedForEntityLoad"] = len(
                    prioritized_results
                )
                stage_detail["entityCandidatesSkipped"] = max(
                    0,
                    len(ranked_results)
                    - len(prioritized_results)
                )

                for search_result in prioritized_results:

                    if not isinstance(
                        search_result,
                        dict
                    ):
                        continue

                    qid = search_result.get(
                        "id"
                    )

                    if not qid:
                        continue

                    if qid in candidate_qids:

                        search_candidates_rejected_duplicate += 1

                        stage_detail["duplicateQidsSkipped"] += 1

                        continue

                    candidate_qids.add(qid)

                    stage_detail["uniqueQids"] += 1

                    entity, entity_error = (
                        cached_entity(
                            qid
                        )
                    )

                    if entity_error:

                        api_error_count += 1

                        api_errors.append({

                            "playerId": player.get(
                                "id"
                            ),

                            "playerName": name,

                            "stage": "entity",

                            "wikidataId": qid,

                            "searchQuery": query,

                            "error": entity_error
                        })

                        continue

                    if not entity:

                        search_entities_missing += 1

                        stage_detail["entitiesMissing"] += 1

                        continue

                    search_entities_loaded += 1

                    stage_detail["entitiesLoaded"] += 1

                    candidate, build_error = (
                        build_candidate_from_entity(
                            player,
                            entity
                        )
                    )

                    if build_error:

                        errors += 1

                        api_errors.append({

                            "playerId": player.get(
                                "id"
                            ),

                            "playerName": name,

                            "stage": "candidate",

                            "wikidataId": qid,

                            "searchQuery": query,

                            "error": build_error
                        })

                        continue

                    if candidate:

                        search_candidates_discovered += 1

                        search_candidates_built += 1

                        stage_detail["candidatesBuilt"] += 1

                        candidate[
                            "searchQuery"
                        ] = query

                        candidate[
                            "searchPass"
                        ] = (
                            "primary"
                            if is_primary
                            else "fallback"
                        )

                        candidates.append(
                            candidate
                        )

                    if REQUEST_DELAY > 0:
                        time.sleep(
                            REQUEST_DELAY
                        )

                print(
                    "    SEARCH "
                    f"{query_index}/{len(search_queries)} "
                    f"{stage_detail['searchPass']} | "
                    f"results={stage_detail['resultCount']} | "
                    f"uniqueQIDs={stage_detail['uniqueQids']} | "
                    f"entities={stage_detail['entitiesLoaded']} | "
                    f"candidates={stage_detail['candidatesBuilt']} | "
                    f"skipped={stage_detail.get('entityCandidatesSkipped', 0)}"
                    + (
                        f" | error={stage_detail['error']}"
                        if stage_detail['error']
                        else ""
                    )
                )

                # ------------------------------------------------
                # SAFE EARLY STOP
                # ------------------------------------------------
                # Do not stop merely because a weak/REVIEW candidate
                # exists. Continue searching unless the best candidate
                # has strong name identity AND football relevance.
                # This preserves V2.1/V2.2 matching strength while
                # reducing false early selections from ambiguous names.

                preliminary = select_best_match(candidates)

                strong_preliminary = (
                    preliminary is not None
                    and preliminary.get("nameScore", 0) >= 94
                    and preliminary.get("footballRelated", False)
                )

                if strong_preliminary:

                    stop_label = (
                        "strong-match-found-in-primary"
                        if is_primary
                        else "strong-match-found-in-fallback"
                    )

                    stage_detail["stopReason"] = stop_label
                    search_stop_reason = stop_label
                    search_early_stops += 1
                    break

            # ------------------------------------------------
            # FINAL MATCH
            # ------------------------------------------------

            match = select_best_match(
                candidates
            )

            # =================================================
            # NO MATCH
            # =================================================

            if not match:

                not_matched += 1

                enriched.append(
                    original
                )

                source_precision = (
                    effective_source_precision(
                        player
                    )
                )

                if source_precision == "year":

                    year_precision_records += 1

                    year_only.append({

                        "id": player.get(
                            "id"
                        ),

                        "name": name,

                        "dateOfBirth": player.get(
                            "dateOfBirth"
                        ),

                        "dateOfBirthPrecision": (
                            "year"
                        ),

                        "matchMethod": None,

                        "wikidataId": None,

                        "classification": (
                            "REVIEW"
                        ),

                        "reason": (
                            "no-suitable-wikidata-match"
                        )
                    })

                # ------------------------------------------------
                # V2.3.1 DIAGNOSTIC
                # ------------------------------------------------

                diagnosis = diagnose_unmatched(
                    player,
                    name,
                    candidates,
                    search_queries,
                    search_errors,
                    search_stage_details,
                    search_stop_reason
                )

                unmatched_diagnostics.append(
                    diagnosis
                )

                print(
                    "    DIAGNOSIS: "
                    + diagnosis[
                        "primaryFailureReason"
                    ]
                )

                if index % 25 == 0:
                    print(
                        f"    PROGRESS: {index:,}/{len(test_players):,} "
                        f"| matched={matched:,} "
                        f"| unmatched={not_matched:,} "
                        f"| API requests={total_api_requests:,} "
                        f"| entities loaded={search_entities_loaded:,}"
                    )

                continue

            matched += 1

            # ------------------------------------------------
            # DISCOVERY
            # ------------------------------------------------

            if (
                match.get(
                    "searchPass"
                )
                == "primary"
            ):

                primary_search_matches += 1

            else:

                fallback_search_matches += 1

            # ------------------------------------------------
            # RESULT
            # ------------------------------------------------

            result = dict(
                player
            )

            result[
                "wikidataId"
            ] = match.get(
                "qid"
            )

            result[
                "matchedName"
            ] = match.get(
                "label"
            )

            result[
                "nameMatchMethod"
            ] = match.get(
                "nameMatchMethod"
            )

            result[
                "matchScore"
            ] = match.get(
                "identityScore"
            )

            result[
                "footballRelated"
            ] = match.get(
                "footballRelated"
            )

            if match.get(
                "dob",
                {}
            ).get(
                "date"
            ):

                result[
                    "wikidataDateOfBirth"
                ] = match[
                    "dob"
                ].get(
                    "date"
                )

                result[
                    "wikidataDatePrecision"
                ] = match[
                    "dob"
                ].get(
                    "precision"
                )

            # ------------------------------------------------
            # DOB DECISION
            # ------------------------------------------------

            dob_result = evaluate_dob(
                player,
                match
            )

            action = dob_result.get(
                "action"
            )

            method = dob_result.get(
                "method"
            )

            classification = (
                dob_result.get(
                    "classification"
                )
            )

            source_precision = (
                effective_source_precision(
                    player
                )
            )

            # ------------------------------------------------
            # CLASSIFICATION
            # ------------------------------------------------

            if classification == "SAFE":

                safe_records += 1

            elif classification == "REVIEW":

                review_records += 1

            elif classification == "REJECT":

                reject_records += 1

            # ------------------------------------------------
            # FULL DOB CORRECTION
            # ------------------------------------------------

            if (
                action == "correct"
                and classification == "SAFE"
            ):

                source_date = player.get(
                    "dateOfBirth"
                )

                new_date = (
                    match.get(
                        "dob",
                        {}
                    ).get(
                        "date"
                    )
                )

                if (
                    new_date
                    and new_date != source_date
                ):

                    result[
                        "sourceDateOfBirth"
                    ] = source_date

                    result[
                        "sourceDatePrecision"
                    ] = source_precision

                    result[
                        "dateOfBirth"
                    ] = new_date

                    result[
                        "dateOfBirthPrecision"
                    ] = "day"

                    corrected_dates += 1

                    corrections.append({

                        "id": player.get(
                            "id"
                        ),

                        "name": name,

                        "sourceDateOfBirth": (
                            source_date
                        ),

                        "sourceDatePrecision": (
                            source_precision
                        ),

                        "correctedDateOfBirth": (
                            new_date
                        ),

                        "correctedDatePrecision": (
                            "day"
                        ),

                        "matchMethod": method,

                        "matchScore": match.get(
                            "identityScore"
                        ),

                        "nameMatchMethod": match.get(
                            "nameMatchMethod"
                        ),

                        "wikidataId": match.get(
                            "qid"
                        ),

                        "classification": (
                            "SAFE"
                        ),

                        "reason": (
                            dob_result.get(
                                "identityReason"
                            )
                        ),

                        "searchQuery": match.get(
                            "searchQuery"
                        ),

                        "searchPass": match.get(
                            "searchPass"
                        )
                    })

            # ------------------------------------------------
            # MONTH CORRECTION
            # ------------------------------------------------

            elif (
                action == "correct-month"
                and classification == "SAFE"
            ):

                source_date = player.get(
                    "dateOfBirth"
                )

                new_date = (
                    match.get(
                        "dob",
                        {}
                    ).get(
                        "date"
                    )
                )

                if (
                    new_date
                    and new_date != source_date
                ):

                    result[
                        "sourceDateOfBirth"
                    ] = source_date

                    result[
                        "sourceDatePrecision"
                    ] = source_precision

                    result[
                        "dateOfBirth"
                    ] = new_date

                    result[
                        "dateOfBirthPrecision"
                    ] = "month"

                    corrected_dates += 1

                    month_precision_records += 1

                    corrections.append({

                        "id": player.get(
                            "id"
                        ),

                        "name": name,

                        "sourceDateOfBirth": (
                            source_date
                        ),

                        "sourceDatePrecision": (
                            source_precision
                        ),

                        "correctedDateOfBirth": (
                            new_date
                        ),

                        "correctedDatePrecision": (
                            "month"
                        ),

                        "matchMethod": method,

                        "matchScore": match.get(
                            "identityScore"
                        ),

                        "nameMatchMethod": match.get(
                            "nameMatchMethod"
                        ),

                        "wikidataId": match.get(
                            "qid"
                        ),

                        "classification": (
                            "SAFE"
                        ),

                        "reason": (
                            dob_result.get(
                                "identityReason"
                            )
                        ),

                        "searchQuery": match.get(
                            "searchQuery"
                        ),

                        "searchPass": match.get(
                            "searchPass"
                        )
                    })

            # ------------------------------------------------
            # CONFLICT
            # ------------------------------------------------

            elif action in (
                "conflict",
                "dob-disagreement"
            ):

                conflict_records += 1

                conflicts.append({

                    "id": player.get(
                        "id"
                    ),

                    "name": name,

                    "sourceDateOfBirth": (
                        player.get(
                            "dateOfBirth"
                        )
                    ),

                    "sourceDatePrecision": (
                        source_precision
                    ),

                    "sourceBirthYear": (
                        get_year(
                            player.get(
                                "dateOfBirth"
                            )
                        )
                    ),

                    "wikidataDateOfBirth": (
                        match.get(
                            "dob",
                            {}
                        ).get(
                            "date"
                        )
                    ),

                    "wikidataDatePrecision": (
                        match.get(
                            "dob",
                            {}
                        ).get(
                            "precision"
                        )
                    ),

                    "wikidataBirthYear": (
                        match.get(
                            "dob",
                            {}
                        ).get(
                            "year"
                        )
                    ),

                    "wikidataId": match.get(
                        "qid"
                    ),

                    "matchedName": match.get(
                        "label"
                    ),

                    "matchedAlias": match.get(
                        "matchedAlias"
                    ),

                    "nameMatchMethod": match.get(
                        "nameMatchMethod"
                    ),

                    "nameScore": match.get(
                        "nameScore"
                    ),

                    "matchScore": match.get(
                        "identityScore"
                    ),

                    "matchMethod": method,

                    "classification": (
                        classification
                    ),

                    "identityReason": (
                        dob_result.get(
                            "identityReason"
                        )
                    ),

                    "action": (
                        "kept-source-dob"
                    ),

                    "searchQuery": match.get(
                        "searchQuery"
                    ),

                    "searchPass": match.get(
                        "searchPass"
                    )
                })

                # ------------------------------------------------
                # V2.3.1 CONFLICT DIAGNOSTIC
                # ------------------------------------------------

                conflict_diagnostics.append(
                    diagnose_conflict(
                        player,
                        name,
                        match,
                        dob_result,
                        candidates
                    )
                )

            # ------------------------------------------------
            # YEAR ONLY
            # ------------------------------------------------

            if (
                effective_source_precision(
                    player
                ) == "year"
                and action not in (
                    "correct",
                    "correct-month"
                )
            ):

                year_precision_records += 1

                year_only.append({

                    "id": player.get(
                        "id"
                    ),

                    "name": name,

                    "dateOfBirth": player.get(
                        "dateOfBirth"
                    ),

                    "dateOfBirthPrecision": (
                        "year"
                    ),

                    "matchMethod": method,

                    "wikidataId": match.get(
                        "qid"
                    ),

                    "wikidataDateOfBirth": (
                        match.get(
                            "dob",
                            {}
                        ).get(
                            "date"
                        )
                    ),

                    "wikidataDatePrecision": (
                        match.get(
                            "dob",
                            {}
                        ).get(
                            "precision"
                        )
                    ),

                    "classification": (
                        classification
                    ),

                    "reason": (
                        dob_result.get(
                            "identityReason"
                        )
                    ),

                    "searchQuery": match.get(
                        "searchQuery"
                    ),

                    "searchPass": match.get(
                        "searchPass"
                    )
                })

            # ------------------------------------------------
            # FULL DOB RECORDS
            # ------------------------------------------------

            if (
                action not in (
                    "correct",
                    "correct-month"
                )
                and effective_source_precision(
                    player
                ) == "day"
            ):

                full_dob_records += 1

            # ------------------------------------------------
            # DECEASED
            # ------------------------------------------------

            if match.get(
                "deathDate"
            ):

                deceased_records += 1

                result[
                    "wikidataDateOfDeath"
                ] = match[
                    "deathDate"
                ]

            # ------------------------------------------------
            # MATCH AUDIT
            # ------------------------------------------------

            matches.append(
                build_match_audit(
                    player,
                    name,
                    match,
                    result,
                    dob_result,
                    source_precision
                )
            )

            enriched.append(
                result
            )

            if index % 25 == 0:
                print(
                    f"    PROGRESS: {index:,}/{len(test_players):,} "
                    f"| matched={matched:,} "
                    f"| unmatched={not_matched:,} "
                    f"| API requests={total_api_requests:,} "
                    f"| entities loaded={search_entities_loaded:,}"
                )

        except Exception as exc:

            errors += 1

            print(
                "    ERROR: "
                f"{type(exc).__name__}: {exc}"
            )

            enriched.append(
                original
            )

            api_errors.append({

                "playerId": player.get(
                    "id"
                ),

                "playerName": name,

                "stage": (
                    "player-processing"
                ),

                "errorType": type(
                    exc
                ).__name__,

                "error": str(
                    exc
                )
            })

    # ========================================================
    # WRITE JSON
    # ========================================================

    def write_json(
        path,
        data
    ):

        with open(
            path,
            "w",
            encoding="utf-8"
        ) as f:

            json.dump(
                data,
                f,
                ensure_ascii=False,
                indent=2
            )

    write_json(
        ENRICHED_FILE,
        enriched
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
        api_errors
    )

    write_json(
        UNMATCHED_DIAGNOSTICS_FILE,
        unmatched_diagnostics
    )

    write_json(
        CONFLICT_DIAGNOSTICS_FILE,
        conflict_diagnostics
    )

    # ========================================================
    # DIAGNOSTIC SUMMARY
    # ========================================================

    unmatched_reason_counts = {}

    for item in unmatched_diagnostics:

        reason = item.get(
            "primaryFailureReason",
            "unknown"
        )

        unmatched_reason_counts[
            reason
        ] = (
            unmatched_reason_counts.get(
                reason,
                0
            ) + 1
        )

    conflict_type_counts = {}

    for item in conflict_diagnostics:

        conflict_type = item.get(
            "conflictType",
            "unknown"
        )

        conflict_type_counts[
            conflict_type
        ] = (
            conflict_type_counts.get(
                conflict_type,
                0
            ) + 1
        )

    diagnostics_summary = {

        "version": VERSION,

        "performanceSettings": {
            "searchLimit": SEARCH_LIMIT,
            "entityLoadLimit": ENTITY_LOAD_LIMIT,
            "maxRetries": MAX_RETRIES,
            "requestDelay": REQUEST_DELAY,
            "strongNameScore": STRONG_NAME_SCORE
        },

        "playersTested": len(
            test_players
        ),

        "matched": matched,

        "notMatched": not_matched,

        "matchRate": (
            round(
                (
                    matched
                    / len(test_players)
                    * 100
                ),
                1
            )
            if test_players
            else 0
        ),

        "safeMatches": safe_records,

        "reviewMatches": review_records,

        "rejectMatches": reject_records,

        "dobCorrections": corrected_dates,

        "dobConflicts": conflict_records,

        "deceasedRecords": deceased_records,

        "unmatchedDiagnostics": len(
            unmatched_diagnostics
        ),

        "conflictDiagnostics": len(
            conflict_diagnostics
        ),

        "unmatchedFailureReasons": (
            unmatched_reason_counts
        ),

        "conflictTypes": (
            conflict_type_counts
        ),

        "discovery": {

            "primarySearchMatches": (
                primary_search_matches
            ),

            "fallbackSearchMatches": (
                fallback_search_matches
            ),

            "fallbackQueriesUsed": (
                fallback_queries_used
            ),

            "totalSearchQueries": (
                total_search_queries
            ),

            "primaryQueriesAttempted": (
                primary_queries_attempted
            ),

            "fallbackQueriesAttempted": (
                fallback_queries_attempted
            ),

            "queriesWithResults": (
                queries_with_results
            ),

            "queriesWithoutResults": (
                queries_without_results
            ),

            "queryErrors": query_errors,

            "uniqueCandidatesDiscovered": (
                search_candidates_discovered
            ),

            "duplicateQidsSkipped": (
                search_candidates_rejected_duplicate
            ),

            "entitiesLoaded": search_entities_loaded,

            "entitiesMissing": search_entities_missing,

            "candidatesBuilt": search_candidates_built,

            "earlyStops": search_early_stops
        },

        "apiDiagnostics": {

            "totalApiRequests": (
                total_api_requests
            ),

            "successfulRequests": (
                successful_requests
            ),

            "retries": retry_count,

            "transientErrors": (
                transient_errors
            ),

            "rateLimitErrors": (
                rate_limit_errors
            ),

            "serverErrors": (
                server_errors
            ),

            "clientErrors": (
                client_errors
            ),

            "timeoutErrors": (
                timeout_errors
            ),

            "connectionErrors": (
                connection_errors
            )
        },

        "httpStatusCodes": (
            http_status_codes
        ),

        "searchCache": {

            "uniqueQueries": len(
                search_cache
            ),

            "cacheHits": (
                search_cache_hits
            ),

            "cacheMisses": (
                search_cache_misses
            )
        },

        "entityCache": {

            "cachedEntities": len(
                entity_cache
            ),

            "cacheHits": (
                entity_cache_hits
            ),

            "cacheMisses": (
                entity_cache_misses
            )
        }
    }

    write_json(
        DIAGNOSTICS_SUMMARY_FILE,
        diagnostics_summary
    )

    # ========================================================
    # REPORT
    # ========================================================

    print()

    print(
        "=" * 60
    )

    print(
        "V2.3.1 FINAL COMPLETE"
    )

    print(
        "=" * 60
    )

    print(
        f"Players tested:        {len(test_players)}"
    )

    print(
        f"Matched:               {matched}"
    )

    print(
        f"Not matched:           {not_matched}"
    )

    print(
        f"Errors:                {errors}"
    )

    print(
        f"API errors:            {api_error_count}"
    )

    print(
        f"SAFE matches:          {safe_records}"
    )

    print(
        f"REVIEW matches:        {review_records}"
    )

    print(
        f"REJECT matches:        {reject_records}"
    )

    print(
        f"Full DOB records:      {full_dob_records}"
    )

    print(
        f"Month precision:       {month_precision_records}"
    )

    print(
        f"Year precision:        {year_precision_records}"
    )

    print(
        f"DOB corrections:       {corrected_dates}"
    )

    print(
        f"DOB conflicts:         {conflict_records}"
    )

    print(
        f"Deceased records:      {deceased_records}"
    )

    print()

    print(
        "DIAGNOSTICS:"
    )

    print(
        f"Unmatched diagnosed:    "
        f"{len(unmatched_diagnostics)}"
    )

    print(
        f"Conflicts diagnosed:    "
        f"{len(conflict_diagnostics)}"
    )

    print()

    print(
        "DISCOVERY:"
    )

    print(
        f"Primary-search matches:   "
        f"{primary_search_matches}"
    )

    print(
        f"Fallback-search matches:  "
        f"{fallback_search_matches}"
    )

    print(
        f"Fallback queries used:    "
        f"{fallback_queries_used}"
    )

    print(
        f"Total search queries:     "
        f"{total_search_queries}"
    )

    print()

    print(
        "SEARCH STAGES:"
    )

    print(
        f"Primary queries attempted:  {primary_queries_attempted}"
    )

    print(
        f"Fallback queries attempted: {fallback_queries_attempted}"
    )

    print(
        f"Queries with results:        {queries_with_results}"
    )

    print(
        f"Queries without results:     {queries_without_results}"
    )

    print(
        f"Query errors:                {query_errors}"
    )

    print(
        f"Unique candidates found:     {search_candidates_discovered}"
    )

    print(
        f"Duplicate QIDs skipped:      {search_candidates_rejected_duplicate}"
    )

    print(
        f"Entities loaded:              {search_entities_loaded}"
    )

    print(
        f"Entities missing:             {search_entities_missing}"
    )

    print(
        f"Candidates built:             {search_candidates_built}"
    )

    print(
        f"Early search stops:           {search_early_stops}"
    )

    print()

    print(
        "API DIAGNOSTICS:"
    )

    print(
        f"Total API requests:       "
        f"{total_api_requests}"
    )

    print(
        f"Successful requests:      "
        f"{successful_requests}"
    )

    print(
        f"Retries:                  "
        f"{retry_count}"
    )

    print(
        f"Transient errors:         "
        f"{transient_errors}"
    )

    print(
        f"Rate-limit errors:        "
        f"{rate_limit_errors}"
    )

    print(
        f"Server errors:            "
        f"{server_errors}"
    )

    print(
        f"Client errors:            "
        f"{client_errors}"
    )

    print(
        f"Timeout errors:           "
        f"{timeout_errors}"
    )

    print(
        f"Connection errors:       "
        f"{connection_errors}"
    )

    print()

    print(
        "HTTP STATUS CODES:"
    )

    for status, count in sorted(
        http_status_codes.items()
    ):

        print(
            f"  HTTP {status}: {count}"
        )

    print()

    print(
        "SEARCH CACHE:"
    )

    print(
        f"Unique queries:           "
        f"{len(search_cache)}"
    )

    print(
        f"Cache hits:               "
        f"{search_cache_hits}"
    )

    print(
        f"Cache misses:             "
        f"{search_cache_misses}"
    )

    print()

    print(
        "ENTITY CACHE:"
    )

    print(
        f"Cached entities:          "
        f"{len(entity_cache)}"
    )

    print(
        f"Cache hits:               "
        f"{entity_cache_hits}"
    )

    print(
        f"Cache misses:             "
        f"{entity_cache_misses}"
    )

    print()

    print(
        "UNMATCHED FAILURE REASONS:"
    )

    if unmatched_reason_counts:

        for reason, count in sorted(
            unmatched_reason_counts.items(),
            key=lambda item: (
                -item[1],
                item[0]
            )
        ):

            print(
                f"  {reason}: {count}"
            )

    else:

        print(
            "  None"
        )

    print()

    print(
        "DOB CONFLICT TYPES:"
    )

    if conflict_type_counts:

        for conflict_type, count in sorted(
            conflict_type_counts.items(),
            key=lambda item: (
                -item[1],
                item[0]
            )
        ):

            print(
                f"  {conflict_type}: {count}"
            )

    else:

        print(
            "  None"
        )

    print()

    print(
        "OUTPUT FILES:"
    )

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

    print()

    print(
        "NEW V2.3.1 DIAGNOSTIC FILES:"
    )

    print(
        f"  {UNMATCHED_DIAGNOSTICS_FILE}"
    )

    print(
        f"  {CONFLICT_DIAGNOSTICS_FILE}"
    )

    print(
        f"  {DIAGNOSTICS_SUMMARY_FILE}"
    )

    print()

    print(
        "=" * 60
    )

    print(
        "NEXT STEP:"
    )

    print(
        "Review unmatched-diagnostics.json "
        "and conflict-diagnostics.json."
    )

    print(
        "Do NOT loosen SAFE rules until the "
        "diagnostic cases have been reviewed."
    )

    print(
        "=" * 60
    )

    print()


# ============================================================
# RUN
# ============================================================

if __name__ == "__main__":

    main()
