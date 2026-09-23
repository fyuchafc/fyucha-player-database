import json
import re
import time
import unicodedata
from pathlib import Path

import requests


# ============================================================
# Fyucha Player Database - Wikidata V2.2.4
# ============================================================

VERSION = "2.2.4"

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

USER_AGENT = (
    "FyuchaPlayerDatabase/2.2.4 "
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
        c for c in value
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
# IMPORTANT:
# DETECT 01-01 YEAR PLACEHOLDERS
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

    # If there is no explicit precision, treat YYYY-01-01
    # as year-only because this is how the source database
    # represents many unknown DOBs.
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
# HTTP REQUEST WITH RETRIES
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

    return data.get(
        "search",
        []
    ), None


# ============================================================
# ENTITY
# ============================================================

def get_entity(qid):

    data, error = request_json(
        ENTITY_URL.format(qid)
    )

    if error:

        return None, error

    return (
        data.get(
            "entities",
            {}
        ).get(
            qid
        ),
        None
    )


# ============================================================
# WIKIDATA TEXT
# ============================================================

def entity_label(entity):

    labels = entity.get(
        "labels",
        {}
    )

    return labels.get(
        "en",
        {}
    ).get(
        "value",
        ""
    )


def entity_aliases(entity):

    aliases = entity.get(
        "aliases",
        {}
    )

    result = []

    for item in aliases.get(
        "en",
        []
    ):

        value = item.get(
            "value"
        )

        if value:
            result.append(
                value
            )

    return result


def entity_description(entity):

    descriptions = entity.get(
        "descriptions",
        {}
    )

    return descriptions.get(
        "en",
        {}
    ).get(
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
# DOB FROM WIKIDATA
# ============================================================

def extract_dob(entity):

    claims = entity.get(
        "claims",
        {}
    )

    dob_claims = claims.get(
        "P569",
        []
    )

    best = None

    precision_rank = {
        "year": 1,
        "month": 2,
        "day": 3
    }

    for claim in dob_claims:

        mainsnak = claim.get(
            "mainsnak",
            {}
        )

        datavalue = mainsnak.get(
            "datavalue"
        )

        if not datavalue:
            continue

        value = datavalue.get(
            "value",
            {}
        )

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
                "rank": rank
            }

    if best:

        return best

    return {
        "date": None,
        "precision": None,
        "year": None,
        "rank": 0
    }


# ============================================================
# DEATH DATE
# ============================================================

def extract_death_date(entity):

    claims = entity.get(
        "claims",
        {}
    )

    for claim in claims.get(
        "P570",
        []
    ):

        datavalue = (
            claim
            .get("mainsnak", {})
            .get("datavalue")
        )

        if not datavalue:
            continue

        value = datavalue.get(
            "value",
            {}
        )

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
# CANDIDATE
# ============================================================

def build_candidate(
    player,
    search_result
):

    qid = search_result.get(
        "id"
    )

    if not qid:
        return None, None

    entity, error = get_entity(
        qid
    )

    if error:

        return None, error

    if not entity:

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

    name_score, name_method = (
        score_name(
            source_name,
            label
        )
    )

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

    # Identity score
    score = name_score

    if football:
        score += 15

    if same_year:
        score += 10

    # Do NOT heavily penalize DOB conflict.
    # Identity and DOB are separate decisions.
    if different_year:
        score -= 5

    return {
        "qid": qid,
        "label": label,
        "aliases": aliases,
        "description": description,
        "nameScore": name_score,
        "nameMatchMethod": name_method,
        "footballRelated": football,
        "dob": dob,
        "sourceYear": source_year,
        "sameYear": same_year,
        "differentYear": different_year,
        "identityScore": score,
        "deathDate": extract_death_date(
            entity
        )
    }, None


# ============================================================
# SELECT MATCH
# ============================================================

def select_best_match(
    candidates
):

    if not candidates:
        return None

    candidates.sort(
        key=lambda c: (
            c["identityScore"],
            c["nameScore"],
            c["sameYear"],
            c["footballRelated"]
        ),
        reverse=True
    )

    best = candidates[0]

    # Strong exact/near exact name
    if best["nameScore"] >= 94:
        return best

    # Strong name + football
    if (
        best["nameScore"] >= 78
        and best["footballRelated"]
    ):
        return best

    # Name + same year
    if (
        best["nameScore"] >= 65
        and best["sameYear"]
    ):
        return best

    return None


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
    ]["date"]

    wikidata_precision = candidate[
        "dob"
    ]["precision"]

    wikidata_year = candidate[
        "dob"
    ]["year"]

    if not wikidata_date:

        return {
            "action": "keep-source",
            "method": "wikidata-dob-unavailable"
        }

    # --------------------------------------------------------
    # SOURCE IS YEAR ONLY
    # --------------------------------------------------------

    if source_precision == "year":

        if (
            source_year is not None
            and wikidata_year == source_year
        ):

            if wikidata_precision == "day":

                return {
                    "action": "correct",
                    "method": "year-to-full-dob"
                }

            if wikidata_precision == "month":

                return {
                    "action": "correct-month",
                    "method": "year-to-month"
                }

            return {
                "action": "keep-source",
                "method": "year-only"
            }

        if (
            source_year is not None
            and wikidata_year is not None
            and source_year != wikidata_year
        ):

            return {
                "action": "conflict",
                "method": "year-conflict"
            }

    # --------------------------------------------------------
    # SOURCE IS MONTH
    # --------------------------------------------------------

    if source_precision == "month":

        source_month = str(
            source_date
        )[:7]

        if wikidata_date.startswith(
            source_month
        ):

            if wikidata_precision == "day":

                return {
                    "action": "correct",
                    "method": "month-to-full-dob"
                }

            return {
                "action": "keep-source",
                "method": "month-match"
            }

        if (
            source_year is not None
            and wikidata_year is not None
            and source_year != wikidata_year
        ):

            return {
                "action": "conflict",
                "method": "year-conflict"
            }

    # --------------------------------------------------------
    # SOURCE IS FULL DAY
    # --------------------------------------------------------

    if source_precision == "day":

        if source_date == wikidata_date:

            return {
                "action": "keep-source",
                "method": "full-date-match"
            }

        if (
            source_year is not None
            and wikidata_year is not None
            and source_year != wikidata_year
        ):

            return {
                "action": "conflict",
                "method": "year-conflict"
            }

        return {
            "action": "dob-disagreement",
            "method": "same-year-dob-disagreement"
        }

    return {
        "action": "keep-source",
        "method": "no-change"
    }


# ============================================================
# MAIN
# ============================================================

def main():

    print()
    print("=" * 60)
    print(
        "Fyucha Player Database - Wikidata V2.2.4"
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

    api_error_count = 0

    # Cache entities during this run
    entity_cache = {}

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

            for search_result in search_results:

                qid = search_result.get(
                    "id"
                )

                if not qid:
                    continue

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

                    if entity:

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
                    continue

                if candidate:
                    candidates.append(
                        candidate
                    )

                time.sleep(
                    0.10
                )

            match = select_best_match(
                candidates
            )

            if not match:

                not_matched += 1

                enriched.append(
                    original
                )

                if (
                    effective_source_precision(
                        player
                    )
                    == "year"
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

                        "matchMethod": None,

                        "wikidataId": None
                    })

                continue

            matched += 1

            result = dict(
                player
            )

            result["wikidataId"] = match[
                "qid"
            ]

            result["matchedName"] = match[
                "label"
            ]

            result["nameMatchMethod"] = match[
                "nameMatchMethod"
            ]

            result["matchScore"] = match[
                "identityScore"
            ]

            result["footballRelated"] = match[
                "footballRelated"
            ]

            if match["dob"]["date"]:

                result[
                    "wikidataDateOfBirth"
                ] = match[
                    "dob"
                ]["date"]

                result[
                    "wikidataDatePrecision"
                ] = match[
                    "dob"
                ]["precision"]

            dob_result = evaluate_dob(
                player,
                match
            )

            action = dob_result[
                "action"
            ]

            method = dob_result[
                "method"
            ]

            source_precision = (
                effective_source_precision(
                    player
                )
            )

            # ------------------------------------------------
            # FULL DOB CORRECTION
            # ------------------------------------------------

            if action == "correct":

                result[
                    "sourceDateOfBirth"
                ] = player.get(
                    "dateOfBirth"
                )

                result[
                    "sourceDatePrecision"
                ] = source_precision

                result[
                    "dateOfBirth"
                ] = match[
                    "dob"
                ]["date"]

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

                    "sourceDateOfBirth": player.get(
                        "dateOfBirth"
                    ),

                    "sourceDatePrecision": source_precision,

                    "correctedDateOfBirth": match[
                        "dob"
                    ]["date"],

                    "correctedDatePrecision": "day",

                    "matchMethod": method,

                    "matchScore": match[
                        "identityScore"
                    ],

                    "nameMatchMethod": match[
                        "nameMatchMethod"
                    ],

                    "wikidataId": match[
                        "qid"
                    ]
                })

            # ------------------------------------------------
            # MONTH CORRECTION
            # ------------------------------------------------

            elif action == "correct-month":

                result[
                    "sourceDateOfBirth"
                ] = player.get(
                    "dateOfBirth"
                )

                result[
                    "sourceDatePrecision"
                ] = source_precision

                result[
                    "dateOfBirth"
                ] = match[
                    "dob"
                ]["date"]

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

                    "sourceDateOfBirth": player.get(
                        "dateOfBirth"
                    ),

                    "sourceDatePrecision": source_precision,

                    "correctedDateOfBirth": match[
                        "dob"
                    ]["date"],

                    "correctedDatePrecision": "month",

                    "matchMethod": method,

                    "matchScore": match[
                        "identityScore"
                    ],

                    "nameMatchMethod": match[
                        "nameMatchMethod"
                    ],

                    "wikidataId": match[
                        "qid"
                    ]
                })

            # ------------------------------------------------
            # DOB CONFLICT
            # ------------------------------------------------

            elif action == "conflict":

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

                    "wikidataDateOfBirth": match[
                        "dob"
                    ]["date"],

                    "wikidataDatePrecision": match[
                        "dob"
                    ]["precision"],

                    "wikidataId": match[
                        "qid"
                    ],

                    "matchedName": match[
                        "label"
                    ],

                    "nameMatchMethod": match[
                        "nameMatchMethod"
                    ],

                    "matchScore": match[
                        "identityScore"
                    ],

                    "matchMethod": "year-conflict",

                    "action": "kept-source-dob"
                })

            # ------------------------------------------------
            # SAME-YEAR DIFFERENT FULL DOB
            # ------------------------------------------------

            elif action == "dob-disagreement":

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

                    "wikidataDateOfBirth": match[
                        "dob"
                    ]["date"],

                    "wikidataDatePrecision": match[
                        "dob"
                    ]["precision"],

                    "wikidataId": match[
                        "qid"
                    ],

                    "matchedName": match[
                        "label"
                    ],

                    "nameMatchMethod": match[
                        "nameMatchMethod"
                    ],

                    "matchScore": match[
                        "identityScore"
                    ],

                    "matchMethod":
                        "same-year-dob-disagreement",

                    "action":
                        "kept-source-dob"
                })

            # ------------------------------------------------
            # YEAR ONLY
            # ------------------------------------------------

            if (
                effective_source_precision(
                    player
                )
                == "year"
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

                    "wikidataId": match[
                        "qid"
                    ]
                })

            # ------------------------------------------------
            # FULL DOB
            # ------------------------------------------------

            if (
                action in (
                    "correct",
                    "correct-month"
                )
            ):

                pass

            elif (
                effective_source_precision(
                    player
                )
                == "day"
            ):

                full_dob_records += 1

            # ------------------------------------------------
            # DECEASED
            # ------------------------------------------------

            if match["deathDate"]:

                deceased_records += 1

                result[
                    "wikidataDateOfDeath"
                ] = match[
                    "deathDate"
                ]

            # ------------------------------------------------
            # AUDIT MATCH
            # ------------------------------------------------

            matches.append({

                "playerId": player.get(
                    "id"
                ),

                "playerName": name,

                "sourceDateOfBirth": player.get(
                    "dateOfBirth"
                ),

                "sourceDatePrecision": source_precision,

                "wikidataId": match[
                    "qid"
                ],

                "matchedName": match[
                    "label"
                ],

                "wikidataDateOfBirth": match[
                    "dob"
                ]["date"],

                "wikidataDatePrecision": match[
                    "dob"
                ]["precision"],

                "finalDateOfBirth": result.get(
                    "dateOfBirth"
                ),

                "finalDatePrecision": result.get(
                    "dateOfBirthPrecision"
                ),

                "matchScore": match[
                    "identityScore"
                ],

                "nameMatchMethod": match[
                    "nameMatchMethod"
                ],

                "matchMethod": method,

                "footballRelated": match[
                    "footballRelated"
                ]
            })

            enriched.append(
                result
            )

        except Exception as exc:

            errors += 1

            print(
                f"    ERROR: {exc}"
            )

            enriched.append(
                original
            )

    # ========================================================
    # WRITE
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
        "V2.2.4 TEST COMPLETE"
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


# ============================================================
# ENTITY CANDIDATE BUILDER
# ============================================================

def build_candidate_from_entity(
    player,
    entity
):

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

    name_score, name_method = (
        score_name(
            source_name,
            label
        )
    )

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

    score = name_score

    if football:
        score += 15

    if same_year:
        score += 10

    if different_year:
        score -= 5

    return {
        "qid": qid,
        "label": label,
        "aliases": aliases,
        "description": description,
        "nameScore": name_score,
        "nameMatchMethod": name_method,
        "footballRelated": football,
        "dob": dob,
        "sourceYear": source_year,
        "sameYear": same_year,
        "differentYear": different_year,
        "identityScore": score,
        "deathDate": extract_death_date(
            entity
        )
    }, None


if __name__ == "__main__":
    main()
