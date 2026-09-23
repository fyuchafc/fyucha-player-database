import json
import re
import time
import unicodedata
from pathlib import Path

import requests


# ============================================================
# Fyucha Player Database - Wikidata V2.2.2
# ============================================================
#
# IMPORTANT DESIGN:
#
# IDENTITY MATCHING and DOB VALIDATION are separate.
#
# A player can be a valid Wikidata match even when Wikidata
# has no usable DOB.
#
# DOB correction is only allowed when:
#
#     source year == Wikidata year
#
# A different birth year NEVER overwrites the source DOB.
#
# ============================================================


VERSION = "2.2.2"

INPUT_FILE = Path("output/players.json")

OUTPUT_DIR = Path("output")

ENRICHED_FILE = OUTPUT_DIR / "enriched-players-test.json"
MATCHES_FILE = OUTPUT_DIR / "wikidata-matches-test.json"
CORRECTIONS_FILE = OUTPUT_DIR / "dob-corrections-test.json"
CONFLICTS_FILE = OUTPUT_DIR / "dob-conflicts-test.json"
YEAR_ONLY_FILE = OUTPUT_DIR / "year-only-test.json"


TEST_LIMIT = 100

REQUEST_DELAY = 0.20

SEARCH_LIMIT = 10

WIKIDATA_SEARCH_API = (
    "https://www.wikidata.org/w/api.php"
)

WIKIDATA_ENTITY_API = (
    "https://www.wikidata.org/wiki/Special:EntityData/{}.json"
)

USER_AGENT = (
    "FyuchaPlayerDatabase/2.2.2 "
    "(football player birthday database)"
)


# ============================================================
# HTTP SESSION
# ============================================================

session = requests.Session()

session.headers.update({
    "User-Agent": USER_AGENT
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
        char
        for char in value
        if not unicodedata.combining(char)
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
    ).strip()

    return value


def compact_name(value):

    return normalize_name(
        value
    ).replace(
        " ",
        ""
    )


def name_tokens(value):

    normalized = normalize_name(
        value
    )

    if not normalized:
        return []

    return normalized.split()


# ============================================================
# NAME MATCHING
# ============================================================

def calculate_name_match(
    source_name,
    wikidata_name
):

    source = normalize_name(
        source_name
    )

    target = normalize_name(
        wikidata_name
    )

    if not source or not target:
        return {
            "score": 0,
            "method": "no-name-match"
        }

    # --------------------------------------------------------
    # Exact normalized name
    # --------------------------------------------------------

    if source == target:

        return {
            "score": 60,
            "method": "exact-name"
        }

    # --------------------------------------------------------
    # Compact exact name
    # --------------------------------------------------------

    if compact_name(source) == compact_name(target):

        return {
            "score": 55,
            "method": "compact-name"
        }

    source_tokens = name_tokens(
        source
    )

    target_tokens = name_tokens(
        target
    )

    source_set = set(
        source_tokens
    )

    target_set = set(
        target_tokens
    )

    common = source_set.intersection(
        target_set
    )

    # --------------------------------------------------------
    # Same complete token set
    # --------------------------------------------------------

    if (
        source_set
        and target_set
        and source_set == target_set
    ):

        return {
            "score": 52,
            "method": "same-token-set"
        }

    # --------------------------------------------------------
    # All source tokens appear in Wikidata name
    #
    # Example:
    #
    # Egidio Arévalo
    # Egidio Arévalo Rios
    # --------------------------------------------------------

    if (
        source_set
        and source_set.issubset(target_set)
    ):

        if len(source_set) >= 2:

            return {
                "score": 45,
                "method": "alias-name"
            }

    # --------------------------------------------------------
    # All Wikidata tokens appear in source name
    # --------------------------------------------------------

    if (
        target_set
        and target_set.issubset(source_set)
    ):

        if len(target_set) >= 2:

            return {
                "score": 45,
                "method": "reverse-alias-name"
            }

    # --------------------------------------------------------
    # Strong partial token match
    # --------------------------------------------------------

    if common:

        ratio = len(common) / max(
            len(source_set),
            len(target_set)
        )

        if ratio >= 0.75:

            return {
                "score": 35,
                "method": "strong-token-match"
            }

        if ratio >= 0.50:

            return {
                "score": 25,
                "method": "partial-token-match"
            }

    return {
        "score": 0,
        "method": "no-name-match"
    }


# ============================================================
# DATE HELPERS
# ============================================================

def get_year(value):

    if not value:
        return None

    match = re.match(
        r"^(\d{4})",
        str(value)
    )

    if not match:
        return None

    return int(
        match.group(1)
    )


def detect_source_precision(player):

    precision = player.get(
        "dateOfBirthPrecision"
    )

    if precision:
        return precision

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


# ============================================================
# WIKIDATA DOB
# ============================================================

def extract_wikidata_dob(entity):

    claims = entity.get(
        "claims",
        {}
    )

    dob_claims = claims.get(
        "P569",
        []
    )

    if not dob_claims:

        return {
            "date": None,
            "precision": None
        }

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

        wikidata_precision = value.get(
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

        # Wikidata precision 11 = day
        if wikidata_precision == 11:

            if month == "00" or day == "00":
                continue

            date = (
                f"{year}-{month}-{day}"
            )

            precision = "day"

        # Wikidata precision 10 = month
        elif wikidata_precision == 10:

            if month == "00":

                date = year
                precision = "year"

            else:

                date = (
                    f"{year}-{month}"
                )

                precision = "month"

        # Wikidata precision 9 = year
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
                "rank": rank
            }

    if best is None:

        return {
            "date": None,
            "precision": None
        }

    return best


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

    try:

        response = session.get(
            WIKIDATA_SEARCH_API,
            params=params,
            timeout=30
        )

        response.raise_for_status()

        data = response.json()

        return data.get(
            "search",
            []
        )

    except Exception as exc:

        print(
            f"    Search error: {exc}"
        )

        return []


# ============================================================
# GET ENTITY
# ============================================================

def get_entity(qid):

    try:

        response = session.get(
            WIKIDATA_ENTITY_API.format(qid),
            timeout=30
        )

        response.raise_for_status()

        data = response.json()

        return data.get(
            "entities",
            {}
        ).get(
            qid
        )

    except Exception as exc:

        print(
            f"    Entity error ({qid}): {exc}"
        )

        return None


# ============================================================
# DESCRIPTION
# ============================================================

def get_description(entity):

    descriptions = entity.get(
        "descriptions",
        {}
    )

    english = descriptions.get(
        "en",
        {}
    )

    return english.get(
        "value",
        ""
    )


# ============================================================
# FOOTBALL DETECTION
# ============================================================

FOOTBALL_TERMS = [
    "footballer",
    "football player",
    "soccer player",
    "association football",
    "football manager",
    "football coach",
    "football midfielder",
    "football defender",
    "football forward",
    "football striker",
    "goalkeeper",
    "midfielder",
    "defender",
    "forward",
    "striker",
    "soccer",
    "football"
]


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
# DEATH DATE
# ============================================================

def extract_death_date(entity):

    claims = entity.get(
        "claims",
        {}
    )

    death_claims = claims.get(
        "P570",
        []
    )

    if not death_claims:
        return None

    for claim in death_claims:

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

        if raw_time:

            return str(
                raw_time
            ).lstrip("+")

    return None


# ============================================================
# BUILD CANDIDATE
# ============================================================

def build_candidate(
    player,
    search_result
):

    qid = search_result.get(
        "id"
    )

    if not qid:
        return None

    entity = get_entity(
        qid
    )

    if not entity:
        return None

    source_name = (
        player.get("displayName")
        or player.get("name")
        or ""
    )

    label = (
        entity
        .get("labels", {})
        .get("en", {})
        .get("value", "")
    )

    description = get_description(
        entity
    )

    name_match = calculate_name_match(
        source_name,
        label
    )

    football_related = football_relevance(
        description
    )

    dob = extract_wikidata_dob(
        entity
    )

    source_date = player.get(
        "dateOfBirth"
    )

    source_year = get_year(
        source_date
    )

    wikidata_year = get_year(
        dob["date"]
    )

    year_match = (
        source_year is not None
        and wikidata_year is not None
        and source_year == wikidata_year
    )

    year_conflict = (
        source_year is not None
        and wikidata_year is not None
        and source_year != wikidata_year
    )

    # --------------------------------------------------------
    # IDENTITY SCORE
    #
    # DOB is NOT required for identity matching.
    # --------------------------------------------------------

    identity_score = (
        name_match["score"]
    )

    if football_related:
        identity_score += 20

    # A matching year is useful confirmation.
    if year_match:
        identity_score += 20

    # A conflicting year is a warning, not an automatic
    # identity rejection.
    if year_conflict:
        identity_score -= 10

    return {
        "qid": qid,
        "label": label,
        "description": description,

        "nameScore": name_match["score"],
        "nameMatchMethod": name_match["method"],

        "footballRelated": football_related,

        "wikidataDateOfBirth": dob["date"],
        "wikidataDatePrecision": dob["precision"],
        "wikidataYear": wikidata_year,

        "sourceYear": source_year,

        "yearMatch": year_match,
        "yearConflict": year_conflict,

        "identityScore": identity_score,

        "deathDate": extract_death_date(
            entity
        )
    }


# ============================================================
# CANDIDATE RANKING
# ============================================================

def rank_candidates(
    player,
    candidates
):

    candidates.sort(
        key=lambda candidate: (
            candidate["identityScore"],
            candidate["nameScore"],
            1 if candidate["footballRelated"] else 0,
            1 if candidate["yearMatch"] else 0
        ),
        reverse=True
    )

    return candidates


# ============================================================
# SELECT IDENTITY MATCH
# ============================================================

def select_identity_match(
    player,
    candidates
):

    if not candidates:
        return None

    ranked = rank_candidates(
        player,
        candidates
    )

    # --------------------------------------------------------
    # Exact / very strong name match
    #
    # This is the key restoration from V2.1.
    # --------------------------------------------------------

    for candidate in ranked:

        if (
            candidate["nameScore"] >= 55
            and candidate["footballRelated"]
        ):

            return candidate

    # --------------------------------------------------------
    # Strong alias name
    # --------------------------------------------------------

    for candidate in ranked:

        if (
            candidate["nameScore"] >= 45
            and candidate["footballRelated"]
        ):

            # If a known DOB year conflicts, do not reject the
            # identity outright, but let the DOB system flag it.
            return candidate

    # --------------------------------------------------------
    # Strong name + matching year
    # --------------------------------------------------------

    for candidate in ranked:

        if (
            candidate["nameScore"] >= 35
            and candidate["yearMatch"]
        ):

            return candidate

    # --------------------------------------------------------
    # Exact name even when football description is absent
    #
    # Some Wikidata entities have weak or missing descriptions.
    # --------------------------------------------------------

    for candidate in ranked:

        if candidate["nameScore"] >= 55:

            return candidate

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

    source_precision = detect_source_precision(
        player
    )

    source_year = get_year(
        source_date
    )

    wikidata_date = candidate[
        "wikidataDateOfBirth"
    ]

    wikidata_precision = candidate[
        "wikidataDatePrecision"
    ]

    wikidata_year = candidate[
        "wikidataYear"
    ]

    # --------------------------------------------------------
    # No Wikidata DOB
    #
    # Keep identity match.
    # Keep original DOB.
    # --------------------------------------------------------

    if not wikidata_date:

        return {
            "action": "keep-source",
            "method": "wikidata-dob-unavailable"
        }

    # --------------------------------------------------------
    # Source year + Wikidata year
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
                "method": "year-only-dob"
            }

        # ----------------------------------------------------
        # DIFFERENT YEAR = CONFLICT
        # ----------------------------------------------------

        if (
            source_year is not None
            and wikidata_year is not None
            and wikidata_year != source_year
        ):

            return {
                "action": "conflict",
                "method": "year-conflict"
            }

    # --------------------------------------------------------
    # Source month
    # --------------------------------------------------------

    if source_precision == "month":

        source_month = str(
            source_date
        )[:7]

        if (
            wikidata_date.startswith(
                source_month
            )
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
    # Source full DOB
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

        # Same year but different day/month:
        #
        # DO NOT overwrite an existing full DOB automatically.
        return {
            "action": "dob-disagreement",
            "method": "same-year-dob-disagreement"
        }

    return {
        "action": "keep-source",
        "method": "no-dob-change"
    }


# ============================================================
# MAIN
# ============================================================

def main():

    print("=" * 60)
    print(
        "Fyucha Player Database - Wikidata V2.2.2"
    )
    print("=" * 60)
    print()

    if not INPUT_FILE.exists():

        raise FileNotFoundError(
            f"Input file not found: {INPUT_FILE}"
        )

    with open(
        INPUT_FILE,
        "r",
        encoding="utf-8"
    ) as file:

        players = json.load(
            file
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

    matched = 0
    not_matched = 0
    full_dob = 0
    month_precision = 0
    year_precision = 0
    corrected = 0
    conflict_count = 0
    deceased = 0
    errors = 0

    # ========================================================
    # PROCESS PLAYERS
    # ========================================================

    for index, player in enumerate(
        test_players,
        start=1
    ):

        original = dict(
            player
        )

        player_name = (
            player.get("displayName")
            or player.get("name")
            or ""
        )

        print(
            f"[{index}/{len(test_players)}] "
            f"{player_name}"
        )

        try:

            search_results = search_wikidata(
                player_name
            )

            candidates = []

            for result in search_results:

                candidate = build_candidate(
                    player,
                    result
                )

                if candidate:

                    candidates.append(
                        candidate
                    )

                time.sleep(
                    REQUEST_DELAY
                )

            match = select_identity_match(
                player,
                candidates
            )

            # ------------------------------------------------
            # NO IDENTITY MATCH
            # ------------------------------------------------

            if not match:

                not_matched += 1

                enriched.append(
                    original
                )

                source_precision = detect_source_precision(
                    player
                )

                if source_precision == "year":

                    year_precision += 1

                    year_only.append({
                        "id": player.get("id"),
                        "name": player_name,
                        "dateOfBirth": player.get(
                            "dateOfBirth"
                        ),
                        "dateOfBirthPrecision": "year",
                        "matchMethod": None,
                        "wikidataId": None
                    })

                continue

            # ------------------------------------------------
            # IDENTITY MATCH FOUND
            # ------------------------------------------------

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

            result["footballRelated"] = match[
                "footballRelated"
            ]

            result["matchScore"] = match[
                "identityScore"
            ]

            # ------------------------------------------------
            # DOB EVALUATION
            # ------------------------------------------------

            dob_result = evaluate_dob(
                player,
                match
            )

            dob_action = dob_result[
                "action"
            ]

            dob_method = dob_result[
                "method"
            ]

            # ------------------------------------------------
            # DOB CONFLICT
            # ------------------------------------------------

            if dob_action == "conflict":

                conflict_count += 1

                conflicts.append({
                    "id": player.get("id"),
                    "name": player_name,

                    "sourceDateOfBirth": player.get(
                        "dateOfBirth"
                    ),

                    "sourceDatePrecision": detect_source_precision(
                        player
                    ),

                    "sourceBirthYear": get_year(
                        player.get(
                            "dateOfBirth"
                        )
                    ),

                    "wikidataId": match[
                        "qid"
                    ],

                    "matchedName": match[
                        "label"
                    ],

                    "wikidataDateOfBirth": match[
                        "wikidataDateOfBirth"
                    ],

                    "wikidataDatePrecision": match[
                        "wikidataDatePrecision"
                    ],

                    "wikidataBirthYear": match[
                        "wikidataYear"
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
            # SAFE FULL DOB CORRECTION
            # ------------------------------------------------

            elif dob_action == "correct":

                original_date = player.get(
                    "dateOfBirth"
                )

                new_date = match[
                    "wikidataDateOfBirth"
                ]

                result[
                    "sourceDateOfBirth"
                ] = original_date

                result[
                    "sourceDatePrecision"
                ] = detect_source_precision(
                    player
                )

                result[
                    "dateOfBirth"
                ] = new_date

                result[
                    "dateOfBirthPrecision"
                ] = "day"

                corrections.append({
                    "id": player.get("id"),
                    "name": player_name,

                    "sourceDateOfBirth": original_date,

                    "sourceDatePrecision": detect_source_precision(
                        player
                    ),

                    "correctedDateOfBirth": new_date,

                    "correctedDatePrecision": "day",

                    "matchMethod": dob_method,

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

                corrected += 1
                full_dob += 1

            # ------------------------------------------------
            # SAFE MONTH CORRECTION
            # ------------------------------------------------

            elif dob_action == "correct-month":

                original_date = player.get(
                    "dateOfBirth"
                )

                new_date = match[
                    "wikidataDateOfBirth"
                ]

                result[
                    "sourceDateOfBirth"
                ] = original_date

                result[
                    "sourceDatePrecision"
                ] = detect_source_precision(
                    player
                )

                result[
                    "dateOfBirth"
                ] = new_date

                result[
                    "dateOfBirthPrecision"
                ] = "month"

                month_precision += 1

                corrections.append({
                    "id": player.get("id"),
                    "name": player_name,

                    "sourceDateOfBirth": original_date,

                    "sourceDatePrecision": detect_source_precision(
                        player
                    ),

                    "correctedDateOfBirth": new_date,

                    "correctedDatePrecision": "month",

                    "matchMethod": dob_method,

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

                corrected += 1

            # ------------------------------------------------
            # YEAR-ONLY
            # ------------------------------------------------

            elif dob_action == "keep-source":

                source_precision = detect_source_precision(
                    player
                )

                if source_precision == "year":

                    year_precision += 1

                    year_only.append({
                        "id": player.get("id"),
                        "name": player_name,
                        "dateOfBirth": player.get(
                            "dateOfBirth"
                        ),
                        "dateOfBirthPrecision": "year",
                        "matchMethod": dob_method,
                        "wikidataId": match[
                            "qid"
                        ]
                    })

            # ------------------------------------------------
            # EXISTING FULL DOB
            # ------------------------------------------------

            elif dob_action == "dob-disagreement":

                # Existing full DOB is retained.
                pass

            # ------------------------------------------------
            # WIKIDATA DOB
            # ------------------------------------------------

            if match[
                "wikidataDateOfBirth"
            ]:

                result[
                    "wikidataDateOfBirth"
                ] = match[
                    "wikidataDateOfBirth"
                ]

                result[
                    "wikidataDatePrecision"
                ] = match[
                    "wikidataDatePrecision"
                ]

            # ------------------------------------------------
            # DECEASED
            # ------------------------------------------------

            if match["deathDate"]:

                deceased += 1

                result[
                    "wikidataDateOfDeath"
                ] = match[
                    "deathDate"
                ]

            # ------------------------------------------------
            # MATCH AUDIT RECORD
            # ------------------------------------------------

            matches.append({
                "playerId": player.get("id"),

                "playerName": player_name,

                "sourceDateOfBirth": player.get(
                    "dateOfBirth"
                ),

                "sourceDatePrecision": detect_source_precision(
                    player
                ),

                "wikidataId": match[
                    "qid"
                ],

                "matchedName": match[
                    "label"
                ],

                "wikidataDateOfBirth": match[
                    "wikidataDateOfBirth"
                ],

                "wikidataDatePrecision": match[
                    "wikidataDatePrecision"
                ],

                "finalDateOfBirth": result.get(
                    "dateOfBirth"
                ),

                "finalDatePrecision": result.get(
                    "dateOfBirthPrecision"
                ),

                "matchScore": match[
                    "identityScore"
                ],

                "matchMethod": dob_method,

                "nameMatchMethod": match[
                    "nameMatchMethod"
                ],

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
    # WRITE OUTPUT FILES
    # ========================================================

    with open(
        ENRICHED_FILE,
        "w",
        encoding="utf-8"
    ) as file:

        json.dump(
            enriched,
            file,
            ensure_ascii=False,
            indent=2
        )

    with open(
        MATCHES_FILE,
        "w",
        encoding="utf-8"
    ) as file:

        json.dump(
            matches,
            file,
            ensure_ascii=False,
            indent=2
        )

    with open(
        CORRECTIONS_FILE,
        "w",
        encoding="utf-8"
    ) as file:

        json.dump(
            corrections,
            file,
            ensure_ascii=False,
            indent=2
        )

    with open(
        CONFLICTS_FILE,
        "w",
        encoding="utf-8"
    ) as file:

        json.dump(
            conflicts,
            file,
            ensure_ascii=False,
            indent=2
        )

    with open(
        YEAR_ONLY_FILE,
        "w",
        encoding="utf-8"
    ) as file:

        json.dump(
            year_only,
            file,
            ensure_ascii=False,
            indent=2
        )

    # ========================================================
    # FINAL REPORT
    # ========================================================

    print()
    print("=" * 60)
    print(
        "V2.2.2 TEST COMPLETE"
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
        f"Full DOB records:      {full_dob}"
    )

    print(
        f"Month precision:       {month_precision}"
    )

    print(
        f"Year precision:        {year_precision}"
    )

    print(
        f"DOB corrections:       {corrected}"
    )

    print(
        f"DOB conflicts:         {conflict_count}"
    )

    print(
        f"Deceased records:      {deceased}"
    )

    if len(test_players) > 0:

        match_rate = (
            matched
            / len(test_players)
            * 100
        )

        print(
            f"Match rate:            {match_rate:.1f}%"
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

    print("=" * 60)


if __name__ == "__main__":
    main()
