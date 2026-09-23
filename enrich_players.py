import json
import re
import time
import unicodedata
from pathlib import Path

import requests


# ============================================================
# Fyucha Player Database - Wikidata V2.2.3
# ============================================================
#
# PURPOSE
# -------
# Identify football players in Wikidata and safely enrich
# their dates of birth.
#
# IMPORTANT:
#
# 1. Identity matching is independent from DOB matching.
# 2. Missing Wikidata DOB does NOT mean the player is unmatched.
# 3. A different DOB year NEVER overwrites the source DOB.
# 4. Year-only DOBs can be upgraded only when the years agree.
# 5. Existing full DOBs are never automatically replaced.
#
# ============================================================


VERSION = "2.2.3"

INPUT_FILE = Path("output/players.json")
OUTPUT_DIR = Path("output")

ENRICHED_FILE = OUTPUT_DIR / "enriched-players-test.json"
MATCHES_FILE = OUTPUT_DIR / "wikidata-matches-test.json"
CORRECTIONS_FILE = OUTPUT_DIR / "dob-corrections-test.json"
CONFLICTS_FILE = OUTPUT_DIR / "dob-conflicts-test.json"
YEAR_ONLY_FILE = OUTPUT_DIR / "year-only-test.json"

TEST_LIMIT = 100

SEARCH_LIMIT = 20

REQUEST_DELAY = 0.15

USER_AGENT = (
    "FyuchaPlayerDatabase/2.2.3 "
    "(football player birthday database)"
)

SEARCH_URL = "https://www.wikidata.org/w/api.php"

ENTITY_URL = (
    "https://www.wikidata.org/wiki/Special:EntityData/{}.json"
)


# ============================================================
# HTTP SESSION
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


def tokens(value):

    value = normalize_name(value)

    if not value:
        return []

    return value.split()


# ============================================================
# SOURCE DOB
# ============================================================

def source_precision(player):

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

    date = str(date)

    if re.fullmatch(r"\d{4}", date):
        return "year"

    if re.fullmatch(r"\d{4}-\d{2}", date):
        return "month"

    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", date):
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

    return int(match.group(1))


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
            SEARCH_URL,
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
            f"    Wikidata search error: {exc}"
        )

        return []


# ============================================================
# ENTITY
# ============================================================

def get_entity(qid):

    try:

        response = session.get(
            ENTITY_URL.format(qid),
            timeout=30
        )

        response.raise_for_status()

        data = response.json()

        return data.get(
            "entities",
            {}
        ).get(qid)

    except Exception as exc:

        print(
            f"    Wikidata entity error: {exc}"
        )

        return None


# ============================================================
# WIKIDATA LABEL
# ============================================================

def entity_label(entity):

    labels = entity.get(
        "labels",
        {}
    )

    english = labels.get(
        "en",
        {}
    )

    return english.get(
        "value",
        ""
    )


# ============================================================
# WIKIDATA ALIASES
# ============================================================

def entity_aliases(entity):

    aliases = entity.get(
        "aliases",
        {}
    )

    english_aliases = aliases.get(
        "en",
        []
    )

    values = []

    for item in english_aliases:

        value = item.get(
            "value"
        )

        if value:
            values.append(value)

    return values


# ============================================================
# NAME SCORE
# ============================================================

def score_name(
    source_name,
    candidate_name
):

    source = normalize_name(
        source_name
    )

    candidate = normalize_name(
        candidate_name
    )

    if not source or not candidate:
        return 0, "no-match"

    # Exact normalized name
    if source == candidate:

        return 100, "exact-name"

    # Exact compact name
    if compact_name(source) == compact_name(candidate):

        return 96, "compact-name"

    source_tokens = set(
        tokens(source)
    )

    candidate_tokens = set(
        tokens(candidate)
    )

    if (
        source_tokens
        and candidate_tokens
        and source_tokens == candidate_tokens
    ):

        return 94, "same-token-set"

    # Source completely contained in candidate
    if (
        source_tokens
        and source_tokens.issubset(candidate_tokens)
    ):

        if len(source_tokens) >= 2:

            return 88, "source-name-contained"

    # Candidate completely contained in source
    if (
        candidate_tokens
        and candidate_tokens.issubset(source_tokens)
    ):

        if len(candidate_tokens) >= 2:

            return 86, "candidate-name-contained"

    # Token overlap
    common = (
        source_tokens
        & candidate_tokens
    )

    if common:

        overlap_source = (
            len(common)
            / max(1, len(source_tokens))
        )

        overlap_candidate = (
            len(common)
            / max(1, len(candidate_tokens))
        )

        overlap = min(
            overlap_source,
            overlap_candidate
        )

        if overlap >= 0.75:

            return 78, "strong-token-overlap"

        if overlap >= 0.50:

            return 65, "partial-token-overlap"

    return 0, "no-match"


# ============================================================
# DOB EXTRACTION
# ============================================================

def extract_dob(entity):

    claims = entity.get(
        "claims",
        {}
    )

    claims = claims.get(
        "P569",
        []
    )

    best = None

    precision_rank = {
        "year": 1,
        "month": 2,
        "day": 3
    }

    for claim in claims:

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

        precision_value = value.get(
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
        if precision_value == 11:

            if month == "00" or day == "00":
                continue

            date = (
                f"{year}-{month}-{day}"
            )

            precision = "day"

        # Wikidata precision 10 = month
        elif precision_value == 10:

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

    death_claims = claims.get(
        "P570",
        []
    )

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
# ENTITY TYPE / OCCUPATION
# ============================================================

def get_occupations(entity):

    claims = entity.get(
        "claims",
        {}
    )

    occupation_claims = claims.get(
        "P106",
        []
    )

    qids = []

    for claim in occupation_claims:

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

        qid = value.get(
            "id"
        )

        if qid:
            qids.append(qid)

    return qids


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
        "")


# ============================================================
# FOOTBALL RELEVANCE
# ============================================================

FOOTBALL_WORDS = (
    "football",
    "soccer",
    "footballer",
    "football player",
    "soccer player",
    "goalkeeper",
    "midfielder",
    "defender",
    "forward",
    "striker",
    "football manager",
    "football coach"
)


def football_relevance(
    description,
    label,
    aliases
):

    combined = " ".join(
        [
            description or "",
            label or "",
            " ".join(aliases)
        ]
    )

    text = normalize_name(
        combined
    )

    for word in FOOTBALL_WORDS:

        if normalize_name(word) in text:

            return True

    return False


# ============================================================
# CANDIDATE
# ============================================================

def make_candidate(
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

    label = entity_label(
        entity
    )

    aliases = entity_aliases(
        entity
    )

    description = get_description(
        entity
    )

    # Score label
    best_name_score, best_name_method = (
        score_name(
            source_name,
            label
        )
    )

    # Score aliases too
    for alias in aliases:

        alias_score, alias_method = (
            score_name(
                source_name,
                alias
            )
        )

        if alias_score > best_name_score:

            best_name_score = alias_score
            best_name_method = (
                f"alias-{alias_method}"
            )

    dob = extract_dob(
        entity
    )

    source_year = get_year(
        player.get("dateOfBirth")
    )

    year_match = (
        source_year is not None
        and dob["year"] is not None
        and source_year == dob["year"]
    )

    year_conflict = (
        source_year is not None
        and dob["year"] is not None
        and source_year != dob["year"]
    )

    football = football_relevance(
        description,
        label,
        aliases
    )

    # --------------------------------------------------------
    # Identity score
    #
    # NAME is the main signal.
    # Football relevance is supporting evidence.
    # DOB year is supporting evidence only.
    # --------------------------------------------------------

    score = best_name_score

    if football:
        score += 15

    if year_match:
        score += 10

    # A conflicting year is a warning.
    # It does NOT automatically destroy a strong name match.
    if year_conflict:
        score -= 5

    return {
        "qid": qid,
        "label": label,
        "aliases": aliases,
        "description": description,

        "nameScore": best_name_score,
        "nameMatchMethod": best_name_method,

        "footballRelated": football,

        "dob": dob,

        "sourceYear": source_year,
        "yearMatch": year_match,
        "yearConflict": year_conflict,

        "identityScore": score,

        "deathDate": extract_death_date(
            entity
        )
    }


# ============================================================
# SELECT BEST MATCH
# ============================================================

def select_match(
    player,
    candidates
):

    if not candidates:
        return None

    candidates.sort(
        key=lambda c: (
            c["identityScore"],
            c["nameScore"],
            1 if c["yearMatch"] else 0,
            1 if c["footballRelated"] else 0
        ),
        reverse=True
    )

    # --------------------------------------------------------
    # VERY STRONG NAME
    #
    # Exact/near-exact identity should be allowed even when
    # Wikidata description is weak or missing.
    # --------------------------------------------------------

    for candidate in candidates:

        if candidate["nameScore"] >= 94:

            return candidate

    # --------------------------------------------------------
    # Strong name + football evidence
    # --------------------------------------------------------

    for candidate in candidates:

        if (
            candidate["nameScore"] >= 78
            and candidate["footballRelated"]
        ):

            return candidate

    # --------------------------------------------------------
    # Strong name + matching DOB year
    # --------------------------------------------------------

    for candidate in candidates:

        if (
            candidate["nameScore"] >= 65
            and candidate["yearMatch"]
        ):

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

    precision = source_precision(
        player
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

    # --------------------------------------------------------
    # No Wikidata DOB
    # --------------------------------------------------------

    if not wikidata_date:

        return {
            "action": "keep-source",
            "method": "wikidata-dob-unavailable"
        }

    # --------------------------------------------------------
    # YEAR SOURCE
    # --------------------------------------------------------

    if precision == "year":

        # Same year
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

        # Different year
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
    # MONTH SOURCE
    # --------------------------------------------------------

    if precision == "month":

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
    # FULL SOURCE DOB
    # --------------------------------------------------------

    if precision == "day":

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

        # Same year but different exact date.
        # Do not overwrite.
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
        "Fyucha Player Database - Wikidata V2.2.3"
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

    matched = 0
    not_matched = 0
    errors = 0

    full_dob_records = 0
    month_precision_records = 0
    year_precision_records = 0

    corrected_dates = 0
    conflict_records = 0
    deceased_records = 0

    # ========================================================
    # LOOP
    # ========================================================

    for index, player in enumerate(
        test_players,
        start=1
    ):

        original = dict(
            player
        )

        name = (
            player.get("displayName")
            or player.get("name")
            or ""
        )

        print(
            f"[{index}/{len(test_players)}] {name}"
        )

        try:

            search_results = search_wikidata(
                name
            )

            candidates = []

            for search_result in search_results:

                candidate = make_candidate(
                    player,
                    search_result
                )

                if candidate:

                    candidates.append(
                        candidate
                    )

                time.sleep(
                    REQUEST_DELAY
                )

            match = select_match(
                player,
                candidates
            )

            # ------------------------------------------------
            # NOT MATCHED
            # ------------------------------------------------

            if not match:

                not_matched += 1

                enriched.append(
                    original
                )

                if (
                    source_precision(player)
                    == "year"
                ):

                    year_precision_records += 1

                    year_only.append({
                        "id": player.get("id"),
                        "name": name,
                        "dateOfBirth": player.get(
                            "dateOfBirth"
                        ),
                        "dateOfBirthPrecision": "year",
                        "matchMethod": None,
                        "wikidataId": None
                    })

                continue

            # ------------------------------------------------
            # MATCH
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

            result["matchScore"] = match[
                "identityScore"
            ]

            result["footballRelated"] = match[
                "footballRelated"
            ]

            if match["aliases"]:

                result["wikidataAliases"] = (
                    match["aliases"]
                )

            if match["dob"]["date"]:

                result["wikidataDateOfBirth"] = (
                    match["dob"]["date"]
                )

                result["wikidataDatePrecision"] = (
                    match["dob"]["precision"]
                )

            # ------------------------------------------------
            # DOB
            # ------------------------------------------------

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

            # ------------------------------------------------
            # SAFE FULL DOB CORRECTION
            # ------------------------------------------------

            if action == "correct":

                result[
                    "sourceDateOfBirth"
                ] = player.get(
                    "dateOfBirth"
                )

                result[
                    "sourceDatePrecision"
                ] = source_precision(
                    player
                )

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

                    "id": player.get("id"),

                    "name": name,

                    "sourceDateOfBirth": player.get(
                        "dateOfBirth"
                    ),

                    "sourceDatePrecision": source_precision(
                        player
                    ),

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
                ] = source_precision(
                    player
                )

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

                    "id": player.get("id"),

                    "name": name,

                    "sourceDateOfBirth": player.get(
                        "dateOfBirth"
                    ),

                    "sourceDatePrecision": source_precision(
                        player
                    ),

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
            # CONFLICT
            # ------------------------------------------------

            elif action == "conflict":

                conflict_records += 1

                conflicts.append({

                    "id": player.get("id"),

                    "name": name,

                    "sourceDateOfBirth": player.get(
                        "dateOfBirth"
                    ),

                    "sourceDatePrecision": source_precision(
                        player
                    ),

                    "sourceBirthYear": get_year(
                        player.get(
                            "dateOfBirth"
                        )
                    ),

                    "correctedDateOfBirth": match[
                        "dob"
                    ]["date"],

                    "correctedDatePrecision": match[
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
            # YEAR-ONLY
            # ------------------------------------------------

            if (
                source_precision(player)
                == "year"
                and action not in (
                    "correct",
                    "correct-month"
                )
            ):

                year_precision_records += 1

                year_only.append({

                    "id": player.get("id"),

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
            # EXISTING FULL DOB
            # ------------------------------------------------

            if (
                source_precision(player)
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
            # MATCH AUDIT
            # ------------------------------------------------

            matches.append({

                "playerId": player.get("id"),

                "playerName": name,

                "sourceDateOfBirth": player.get(
                    "dateOfBirth"
                ),

                "sourceDatePrecision": source_precision(
                    player
                ),

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
    # WRITE FILES
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

    # ========================================================
    # REPORT
    # ========================================================

    print()
    print("=" * 60)
    print(
        "V2.2.3 TEST COMPLETE"
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

    print("=" * 60)


if __name__ == "__main__":
    main()
