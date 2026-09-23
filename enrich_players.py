import json
import re
import time
import unicodedata
from pathlib import Path

import requests


# ============================================================
# Fyucha Player Database - Wikidata V2.2.6
#
# Workflow:
# MATCH -> CLASSIFY -> APPLY
#
# SAFE:
#   Strong identity + same birth year + precise Wikidata DOB
#
# REVIEW:
#   Identity or DOB evidence is not strong enough for automatic
#   correction.
#
# REJECT:
#   Clear identity conflict or birth-year conflict.
#
# Important:
#   YYYY-01-01 + year precision = YEAR ONLY.
#   It must never be treated as a confirmed January 1 birthday.
#
# V2.2.6:
#   SAFE / REVIEW / REJECT logic preserved from V2.2.5.
# ============================================================

VERSION = "2.2.6"

INPUT_FILE = Path("output/players.json")
OUTPUT_DIR = Path("output")

ENRICHED_FILE = OUTPUT_DIR / "enriched-players-test.json"
MATCHES_FILE = OUTPUT_DIR / "wikidata-matches-test.json"
CORRECTIONS_FILE = OUTPUT_DIR / "dob-corrections-test.json"
CONFLICTS_FILE = OUTPUT_DIR / "dob-conflicts-test.json"
YEAR_ONLY_FILE = OUTPUT_DIR / "year-only-test.json"
ERRORS_FILE = OUTPUT_DIR / "wikidata-errors-test.json"

TEST_LIMIT = 100

SEARCH_LIMIT = 10

MAX_RETRIES = 4

BASE_DELAY = 1.0

REQUEST_DELAY = 0.10

USER_AGENT = (
    "FyuchaPlayerDatabase/2.2.6 "
    "(football player birthday database)"
)

SEARCH_URL = "https://www.wikidata.org/w/api.php"

ENTITY_URL = (
    "https://www.wikidata.org/wiki/Special:EntityData/{}.json"
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

    value = value.replace("’", "'")
    value = value.replace("'", "")
    value = value.replace("-", " ")

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
    ).replace(" ", "")


def name_tokens(value):

    value = normalize_name(value)

    if not value:
        return set()

    return set(
        value.split()
    )


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

    date = str(date)

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
#
# YYYY-01-01 is considered year-only when:
#
#   dateOfBirthPrecision == "year"
#
# OR when there is no explicit precision.
#
# This protects the source database from treating placeholder
# dates as real January 1 birthdays.
# ============================================================

def is_likely_year_placeholder(player):

    date = player.get(
        "dateOfBirth"
    )

    if not date:
        return False

    date = str(date)

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
# HTTP REQUEST
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
                    f"    Rate limited. "
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
                    f"    Wikidata server error "
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

def search_wikidata(name):

    if not name:
        return [], None

    params = {
        "action": "wbsearchentities",
        "search": name,
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
# WIKIDATA TEXT
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
# NAME MATCH
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

    if source_normalized == candidate_normalized:

        return 100, "exact-name"

    if compact_name(source) == compact_name(candidate):

        return 96, "compact-name"

    source_tokens = name_tokens(
        source
    )

    candidate_tokens = name_tokens(
        candidate
    )

    if source_tokens == candidate_tokens:

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

            return 85, "strong-token-overlap"

        if ratio >= 0.50:

            return 70, "partial-token-overlap"

    return 0, "no-match"


# ============================================================
# WIKIDATA DOB
#
# IMPORTANT:
# Wikidata precision is taken from the claim itself.
#
# precision 9  = year
# precision 10 = month
# precision 11 = day
#
# A date string ending in 01-01 is NOT automatically considered
# a full day-level DOB.
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
        ).lstrip("+")

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

            # Unknown Wikidata precision.
            # Do not pretend it is day precision.
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
                "precisionNumber": precision_number
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

        datavalue = (
            claim
            .get("mainsnak", {})
            .get("datavalue")
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
            ).lstrip("+")

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

        if normalize_name(term) in text:

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
    # NAME SCORE
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
    #
    # DOB conflict is deliberately not used to prove identity.
    # A wrong person can have an apparently useful DOB.
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
# SELECT BEST MATCH
# ============================================================

def select_best_match(
    candidates
):

    if not candidates:
        return None

    valid = [
        c
        for c in candidates
        if isinstance(c, dict)
    ]

    if not valid:
        return None

    valid.sort(
        key=lambda c: (
            c.get("identityScore", 0),
            c.get("nameScore", 0),
            c.get("sameYear", False),
            c.get("footballRelated", False)
        ),
        reverse=True
    )

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

    # --------------------------------------------------------
    # STRONG EXACT / NEAR EXACT NAME
    # --------------------------------------------------------

    if name_score >= 94:
        return best

    # --------------------------------------------------------
    # STRONG NAME + FOOTBALL
    # --------------------------------------------------------

    if (
        name_score >= 78
        and football
    ):
        return best

    # --------------------------------------------------------
    # NAME + SAME YEAR
    # --------------------------------------------------------

    if (
        name_score >= 65
        and same_year
    ):
        return best

    return None


# ============================================================
# IDENTITY CLASSIFICATION
# ============================================================

def classify_identity(
    candidate
):

    if not candidate:
        return "REJECT", "no-candidate"

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

    # --------------------------------------------------------
    # CLEAR NAME FAILURE
    # --------------------------------------------------------

    if name_score < 65:

        return (
            "REJECT",
            "weak-name-match"
        )

    # --------------------------------------------------------
    # VERY STRONG EXACT IDENTITY
    # --------------------------------------------------------

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

    # --------------------------------------------------------
    # STRONG NAME + FOOTBALL
    # --------------------------------------------------------

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

    # --------------------------------------------------------
    # NAME + SAME YEAR
    # --------------------------------------------------------

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

    wikidata_date = candidate[
        "dob"
    ].get(
        "date"
    )

    wikidata_precision = candidate[
        "dob"
    ].get(
        "precision"
    )

    wikidata_year = candidate[
        "dob"
    ].get(
        "year"
    )

    identity_classification, identity_reason = (
        classify_identity(
            candidate
        )
    )

    base = {
        "classification": identity_classification,
        "identityReason": identity_reason,
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
    #
    # NEVER replace the source year automatically.
    # --------------------------------------------------------

    if (
        source_year is not None
        and wikidata_year is not None
        and source_year != wikidata_year
    ):

        # Strong football identity + exact/near-exact name,
        # but contradictory year.
        #
        # This is a genuine audit issue, not a correction.
        if (
            candidate.get("nameScore", 0) >= 94
            and candidate.get("footballRelated", False)
        ):

            base["classification"] = "REJECT"

            base["identityReason"] = (
                "strong-identity-year-conflict"
            )

        else:

            base["classification"] = "REVIEW"

            base["identityReason"] = (
                "possible-identity-year-conflict"
            )

        base["action"] = "conflict"

        base["method"] = (
            "year-conflict"
        )

        return base

    # --------------------------------------------------------
    # SOURCE YEAR ONLY
    # --------------------------------------------------------

    if source_precision == "year":

        # Only SAFE identity can automatically upgrade the DOB.
        if identity_classification != "SAFE":

            if wikidata_year == source_year:

                base["action"] = (
                    "keep-source"
                )

                base["method"] = (
                    "same-year-review"
                )

            else:

                base["action"] = (
                    "keep-source"
                )

                base["method"] = (
                    "year-only"
                )

            return base

        # ----------------------------------------------------
        # SAME YEAR + DAY PRECISION
        # ----------------------------------------------------

        if (
            wikidata_year == source_year
            and wikidata_precision == "day"
        ):

            # Genuine precision improvement.
            if wikidata_date != source_date:

                base["action"] = (
                    "correct"
                )

                base["method"] = (
                    "year-to-full-dob"
                )

                return base

            # Defensive: never create a fake correction.
            base["action"] = (
                "keep-source"
            )

            base["method"] = (
                "same-date"
            )

            return base

        # ----------------------------------------------------
        # SAME YEAR + MONTH PRECISION
        # ----------------------------------------------------

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

        # ----------------------------------------------------
        # SAME YEAR + YEAR ONLY
        # ----------------------------------------------------

        base["action"] = (
            "keep-source"
        )

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

            base["action"] = (
                "keep-source"
            )

            base["method"] = (
                "month-match"
            )

            return base

        base["action"] = (
            "keep-source"
        )

        base["method"] = (
            "month-review"
        )

        return base

    # --------------------------------------------------------
    # SOURCE FULL DAY
    # --------------------------------------------------------

    if source_precision == "day":

        if source_date == wikidata_date:

            base["action"] = (
                "keep-source"
            )

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

                base["classification"] = "REVIEW"

            return base

        base["action"] = (
            "conflict"
        )

        base["method"] = (
            "year-conflict"
        )

        base["classification"] = "REJECT"

        return base

    # --------------------------------------------------------
    # UNKNOWN SOURCE PRECISION
    # --------------------------------------------------------

    base["classification"] = "REVIEW"

    base["identityReason"] = (
        "source-dob-precision-unknown"
    )

    base["action"] = (
        "keep-source"
    )

    base["method"] = (
        "precision-unknown"
    )

    return base


# ============================================================
# AUDIT RECORD
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

        "sourceDatePrecision": source_precision,

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
            match.get("dob", {})
            .get("date")
        ),

        "wikidataDatePrecision": (
            match.get("dob", {})
            .get("precision")
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
        )
    }


# ============================================================
# MAIN
# ============================================================

def main():

    print()
    print("=" * 60)
    print(
        f"Fyucha Player Database - Wikidata V{VERSION}"
    )
    print("=" * 60)
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

        players = json.load(f)

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

    test_players = players[
        :TEST_LIMIT
    ]

    print(
        f"Testing first {len(test_players)} players."
    )

    print()

    OUTPUT_DIR.mkdir(
        parents=True,
        exist_ok=True
    )

    enriched = []
    matches = []
    corrections = []
    conflicts = []
    year_only = []
    api_errors = []

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

    entity_cache = {}

    # ========================================================
    # PROCESS PLAYERS
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
            # SEARCH
            # ------------------------------------------------

            search_results, search_error = (
                search_wikidata(
                    name
                )
            )

            if search_error:

                api_error_count += 1

                api_errors.append({

                    "playerId": player.get(
                        "id"
                    ),

                    "playerName": name,

                    "stage": "search",

                    "error": search_error
                })

                enriched.append(
                    original
                )

                continue

            candidates = []

            # ------------------------------------------------
            # BUILD CANDIDATES
            # ------------------------------------------------

            for search_result in search_results:

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

                # Cache entity.
                if qid in entity_cache:

                    entity = entity_cache[
                        qid
                    ]

                    entity_error = None

                else:

                    entity, entity_error = (
                        get_entity(
                            qid
                        )
                    )

                    if entity is not None:

                        entity_cache[
                            qid
                        ] = entity

                if entity_error:

                    api_error_count += 1

                    api_errors.append({

                        "playerId": player.get(
                            "id"
                        ),

                        "playerName": name,

                        "stage": "entity",

                        "wikidataId": qid,

                        "error": entity_error
                    })

                    continue

                if not entity:
                    continue

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

                        "error": build_error
                    })

                    continue

                if candidate:

                    candidates.append(
                        candidate
                    )

                time.sleep(
                    REQUEST_DELAY
                )

            # ------------------------------------------------
            # SELECT MATCH
            # ------------------------------------------------

            match = select_best_match(
                candidates
            )

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

                        "dateOfBirthPrecision": "year",

                        "matchMethod": None,

                        "wikidataId": None,

                        "classification": "REVIEW",

                        "reason": "no-suitable-wikidata-match"
                    })

                continue

            matched += 1

            # ------------------------------------------------
            # RESULT BASE
            # ------------------------------------------------

            result = dict(
                player
            )

            result["wikidataId"] = match.get(
                "qid"
            )

            result["matchedName"] = match.get(
                "label"
            )

            result["nameMatchMethod"] = match.get(
                "nameMatchMethod"
            )

            result["matchScore"] = match.get(
                "identityScore"
            )

            result["footballRelated"] = match.get(
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

            classification = dob_result.get(
                "classification"
            )

            source_precision = (
                effective_source_precision(
                    player
                )
            )

            # ------------------------------------------------
            # CLASSIFICATION COUNTERS
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

                new_date = match[
                    "dob"
                ].get(
                    "date"
                )

                # Absolute safety check:
                # only record an actual change.
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

                    full_dob_records += 1

                    corrections.append({

                        "id": player.get(
                            "id"
                        ),

                        "name": name,

                        "sourceDateOfBirth": source_date,

                        "sourceDatePrecision": source_precision,

                        "correctedDateOfBirth": new_date,

                        "correctedDatePrecision": "day",

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

                        "classification": "SAFE",

                        "reason": dob_result.get(
                            "identityReason"
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

                new_date = match[
                    "dob"
                ].get(
                    "date"
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

                        "sourceDateOfBirth": source_date,

                        "sourceDatePrecision": source_precision,

                        "correctedDateOfBirth": new_date,

                        "correctedDatePrecision": "month",

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

                        "classification": "SAFE",

                        "reason": dob_result.get(
                            "identityReason"
                        )
                    })

            # ------------------------------------------------
            # DOB CONFLICT
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

                    "sourceDateOfBirth": player.get(
                        "dateOfBirth"
                    ),

                    "sourceDatePrecision": source_precision,

                    "sourceBirthYear": get_year(
                        player.get(
                            "dateOfBirth"
                        )
                    ),

                    "wikidataDateOfBirth": (
                        match.get("dob", {})
                        .get("date")
                    ),

                    "wikidataDatePrecision": (
                        match.get("dob", {})
                        .get("precision")
                    ),

                    "wikidataBirthYear": (
                        match.get("dob", {})
                        .get("year")
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

                    "classification": classification,

                    "identityReason": dob_result.get(
                        "identityReason"
                    ),

                    "action": "kept-source-dob"
                })

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

                    "dateOfBirthPrecision": "year",

                    "matchMethod": method,

                    "wikidataId": match.get(
                        "qid"
                    ),

                    "wikidataDateOfBirth": (
                        match.get("dob", {})
                        .get("date")
                    ),

                    "wikidataDatePrecision": (
                        match.get("dob", {})
                        .get("precision")
                    ),

                    "classification": classification,

                    "reason": dob_result.get(
                        "identityReason"
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

        except Exception as exc:

            errors += 1

            print(
                f"    ERROR: {type(exc).__name__}: {exc}"
            )

            enriched.append(
                original
            )

            api_errors.append({

                "playerId": player.get(
                    "id"
                ),

                "playerName": name,

                "stage": "player-processing",

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

    # ========================================================
    # REPORT
    # ========================================================

    print()
    print("=" * 60)
    print(
        "V2.2.6 TEST COMPLETE"
    )
    print("=" * 60)

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

    if test_players:

        rate = (
            matched
            / len(test_players)
            * 100
        )

        print(
            f"Match rate:            {rate:.1f}%"
        )

    print()
    print(
        "Output files:"
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

    print("=" * 60)

    print()
    print(
        "IMPORTANT:"
    )

    print(
        "Review dob-corrections-test.json and "
        "dob-conflicts-test.json before running "
        "V2.2.6 against the full database."
    )


# ============================================================
# RUN
# ============================================================

if __name__ == "__main__":
    main()
