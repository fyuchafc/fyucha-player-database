import json
import re
import time
import unicodedata
from pathlib import Path

import requests


# ============================================================
# Fyucha Player Database - Wikidata V2.2.1
# ============================================================
#
# V2.2.1
#
# Main goals:
#
# 1. Preserve V2.1's broad matching behavior.
# 2. Improve candidate scoring.
# 3. Prevent incorrect DOB year changes.
# 4. Record suspicious year conflicts separately.
# 5. Upgrade year-only DOBs only when the Wikidata year agrees.
#
# ============================================================


INPUT_FILE = Path("output/players.json")

ENRICHED_FILE = Path("output/enriched-players-test.json")
MATCHES_FILE = Path("output/wikidata-matches-test.json")
CORRECTIONS_FILE = Path("output/dob-corrections-test.json")
CONFLICTS_FILE = Path("output/dob-conflicts-test.json")
YEAR_ONLY_FILE = Path("output/year-only-test.json")

WIKIDATA_SEARCH_API = "https://www.wikidata.org/w/api.php"
WIKIDATA_ENTITY_API = "https://www.wikidata.org/wiki/Special:EntityData/{}.json"

TEST_LIMIT = 100

REQUEST_DELAY = 0.20

USER_AGENT = (
    "FyuchaPlayerDatabase/2.2.1 "
    "(football player birthday database)"
)


# ============================================================
# HTTP
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

    value = unicodedata.normalize(
        "NFKD",
        str(value)
    )

    value = "".join(
        c for c in value
        if not unicodedata.combining(c)
    )

    value = value.lower()

    value = value.replace("’", "'")
    value = value.replace("–", "-")
    value = value.replace("—", "-")

    value = re.sub(
        r"[^a-z0-9\s-]",
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

    return normalize_name(value).replace(
        " ",
        ""
    )


# ============================================================
# NAME SCORE
# ============================================================

def name_score(source, target):

    source_normalized = normalize_name(source)
    target_normalized = normalize_name(target)

    if not source_normalized or not target_normalized:
        return 0

    if source_normalized == target_normalized:
        return 60

    if compact_name(source) == compact_name(target):
        return 55

    source_parts = set(
        source_normalized.split()
    )

    target_parts = set(
        target_normalized.split()
    )

    if source_parts and target_parts:

        common = source_parts.intersection(
            target_parts
        )

        if common:

            ratio = len(common) / max(
                len(source_parts),
                len(target_parts)
            )

            if ratio >= 1:
                return 50

            if ratio >= 0.75:
                return 40

            if ratio >= 0.5:
                return 25

    if (
        source_normalized in target_normalized
        or target_normalized in source_normalized
    ):
        return 15

    return 0


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

    if match:
        return int(match.group(1))

    return None


def parse_source_precision(player):

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

    if re.fullmatch(
        r"\d{4}",
        str(date)
    ):
        return "year"

    if re.fullmatch(
        r"\d{4}-\d{2}",
        str(date)
    ):
        return "month"

    if re.fullmatch(
        r"\d{4}-\d{2}-\d{2}",
        str(date)
    ):
        return "day"

    return None


# ============================================================
# WIKIDATA DATE
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

    if not dob_claims:
        return None, None

    best = None

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

        time_value = value.get(
            "time"
        )

        precision = value.get(
            "precision"
        )

        if not time_value:
            continue

        time_value = str(
            time_value
        ).lstrip("+")

        match = re.match(
            r"(\d{4})-(\d{2})-(\d{2})",
            time_value
        )

        if not match:
            continue

        year = match.group(1)
        month = match.group(2)
        day = match.group(3)

        if precision == 11:

            if month == "00" or day == "00":
                continue

            candidate = (
                f"{year}-{month}-{day}"
            )

            candidate_precision = "day"

        elif precision == 10:

            if month == "00":
                candidate = year
                candidate_precision = "year"

            else:
                candidate = (
                    f"{year}-{month}"
                )
                candidate_precision = "month"

        else:

            candidate = year
            candidate_precision = "year"

        # Prefer the most precise DOB.
        rank = {
            "year": 1,
            "month": 2,
            "day": 3
        }.get(
            candidate_precision,
            0
        )

        if best is None or rank > best[0]:

            best = (
                rank,
                candidate,
                candidate_precision
            )

    if best is None:
        return None, None

    return best[1], best[2]


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
        "limit": 10
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
            f"    Wikidata search error: {exc}"
        )

        return []


# ============================================================
# WIKIDATA ENTITY
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
            f"    Entity error for {qid}: {exc}"
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
# FOOTBALL RELEVANCE
# ============================================================

FOOTBALL_TERMS = [
    "footballer",
    "football player",
    "soccer player",
    "association football",
    "football manager",
    "football coach",
    "goalkeeper",
    "midfielder",
    "defender",
    "forward",
    "striker",
    "football",
    "soccer"
]


def football_score(description):

    text = normalize_name(
        description
    )

    score = 0

    for term in FOOTBALL_TERMS:

        if normalize_name(term) in text:

            score += 5

    return min(
        score,
        20
    )


# ============================================================
# DECEASED
# ============================================================

def get_death_date(entity):

    claims = entity.get(
        "claims",
        {}
    )

    claims = claims.get(
        "P570",
        []
    )

    if not claims:
        return None

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
            "value"
        )

        if isinstance(
            value,
            dict
        ):

            return value.get(
                "time"
            )

    return None


# ============================================================
# EVALUATE CANDIDATE
# ============================================================

def evaluate_candidate(
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
        entity.get(
            "labels",
            {}
        )
        .get(
            "en",
            {}
        )
        .get(
            "value",
            ""
        )
    )

    description = get_description(
        entity
    )

    source_date = player.get(
        "dateOfBirth"
    )

    source_precision = parse_source_precision(
        player
    )

    source_year = get_year(
        source_date
    )

    wikidata_date, wikidata_precision = (
        extract_dob(entity)
    )

    wikidata_year = get_year(
        wikidata_date
    )

    score = name_score(
        source_name,
        label
    )

    score += football_score(
        description
    )

    year_match = False
    year_conflict = False

    if (
        source_year
        and wikidata_year
    ):

        if source_year == wikidata_year:

            year_match = True

            # Strong confirmation
            score += 30

        else:

            year_conflict = True

            # Penalize but DO NOT automatically discard.
            # This allows us to report suspicious candidates.
            score -= 20

    return {
        "qid": qid,
        "label": label,
        "description": description,
        "entity": entity,
        "wikidataDate": wikidata_date,
        "wikidataPrecision": wikidata_precision,
        "wikidataYear": wikidata_year,
        "sourceYear": source_year,
        "nameScore": name_score(
            source_name,
            label
        ),
        "footballScore": football_score(
            description
        ),
        "score": score,
        "yearMatch": year_match,
        "yearConflict": year_conflict,
        "deathDate": get_death_date(
            entity
        )
    }


# ============================================================
# FIND BEST CANDIDATE
# ============================================================

def find_candidates(player):

    source_name = (
        player.get("displayName")
        or player.get("name")
        or ""
    )

    results = search_wikidata(
        source_name
    )

    candidates = []

    for result in results:

        candidate = evaluate_candidate(
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

    candidates.sort(
        key=lambda x: x["score"],
        reverse=True
    )

    return candidates


# ============================================================
# SELECT SAFE MATCH
# ============================================================

def select_match(
    player,
    candidates
):

    if not candidates:
        return None

    source_precision = parse_source_precision(
        player
    )

    source_year = get_year(
        player.get(
            "dateOfBirth"
        )
    )

    # --------------------------------------------------------
    # YEAR-ONLY SOURCE
    # --------------------------------------------------------

    if (
        source_precision == "year"
        and source_year
    ):

        same_year = [
            c for c in candidates
            if c["yearMatch"]
        ]

        if same_year:

            # Prefer strong name matches.
            same_year.sort(
                key=lambda x: (
                    x["nameScore"],
                    x["footballScore"],
                    x["score"]
                ),
                reverse=True
            )

            best = same_year[0]

            # Require meaningful name similarity.
            if best["nameScore"] >= 25:

                return best

        return None

    # --------------------------------------------------------
    # FULL DATE SOURCE
    # --------------------------------------------------------

    if source_precision == "day":

        source_date = player.get(
            "dateOfBirth"
        )

        exact = [
            c for c in candidates
            if c["wikidataDate"] == source_date
        ]

        if exact:

            exact.sort(
                key=lambda x: x["score"],
                reverse=True
            )

            return exact[0]

        # If we don't have exact DOB agreement,
        # allow a strong name + same year match.
        same_year = [
            c for c in candidates
            if (
                c["yearMatch"]
                and c["nameScore"] >= 40
            )
        ]

        if same_year:

            same_year.sort(
                key=lambda x: x["score"],
                reverse=True
            )

            return same_year[0]

        return None

    # --------------------------------------------------------
    # MONTH / UNKNOWN
    # --------------------------------------------------------

    same_year = [
        c for c in candidates
        if c["yearMatch"]
    ]

    if same_year:

        same_year.sort(
            key=lambda x: x["score"],
            reverse=True
        )

        if same_year[0]["nameScore"] >= 25:

            return same_year[0]

    # General fallback for strong exact name matches.
    strong_name = [
        c for c in candidates
        if c["nameScore"] >= 50
    ]

    if strong_name:

        strong_name.sort(
            key=lambda x: x["score"],
            reverse=True
        )

        return strong_name[0]

    return None


# ============================================================
# MATCH METHOD
# ============================================================

def determine_match_method(
    source_precision,
    wikidata_precision,
    source_date,
    wikidata_date
):

    if not wikidata_date:
        return None

    source_year = get_year(
        source_date
    )

    wikidata_year = get_year(
        wikidata_date
    )

    if (
        source_precision == "year"
        and source_year == wikidata_year
    ):

        if wikidata_precision == "day":
            return "year-to-full-dob"

        if wikidata_precision == "month":
            return "year-to-month"

        return "year-only"

    if (
        source_precision == "month"
        and source_year == wikidata_year
    ):

        if wikidata_precision == "day":
            return "month-to-full-dob"

        return "month-only"

    if (
        source_precision == "day"
        and source_date == wikidata_date
    ):

        return "full-date-match"

    return None


# ============================================================
# MAIN
# ============================================================

def main():

    print("=" * 60)
    print(
        "Fyucha Player Database - Wikidata V2.2.1"
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

    enriched = []

    matches = []

    corrections = []

    conflicts = []

    year_only = []

    matched = 0
    full_dob = 0
    month_precision = 0
    year_precision = 0
    corrected = 0
    conflict_count = 0
    deceased = 0
    errors = 0

    # ========================================================
    # PROCESS
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
            or "Unknown"
        )

        source_date = player.get(
            "dateOfBirth"
        )

        source_precision = parse_source_precision(
            player
        )

        print(
            f"[{index}/{len(test_players)}] {name}"
        )

        try:

            candidates = find_candidates(
                player
            )

            best = select_match(
                player,
                candidates
            )

            # ------------------------------------------------
            # NO SAFE MATCH
            # ------------------------------------------------

            if not best:

                # Check for suspicious candidates.
                source_year = get_year(
                    source_date
                )

                conflicting = []

                if source_year:

                    for candidate in candidates:

                        if (
                            candidate["yearConflict"]
                            and candidate["nameScore"] >= 25
                        ):

                            conflicting.append(
                                candidate
                            )

                if conflicting:

                    conflicting.sort(
                        key=lambda x: x["score"],
                        reverse=True
                    )

                    candidate = conflicting[0]

                    conflicts.append({
                        "id": player.get("id"),
                        "name": name,
                        "sourceDateOfBirth": source_date,
                        "sourceDatePrecision": source_precision,
                        "sourceBirthYear": source_year,
                        "candidateDateOfBirth": candidate[
                            "wikidataDate"
                        ],
                        "candidateDatePrecision": candidate[
                            "wikidataPrecision"
                        ],
                        "candidateBirthYear": candidate[
                            "wikidataYear"
                        ],
                        "matchedName": candidate[
                            "label"
                        ],
                        "wikidataId": candidate[
                            "qid"
                        ],
                        "nameScore": candidate[
                            "nameScore"
                        ],
                        "matchScore": candidate[
                            "score"
                        ],
                        "matchMethod": "year-conflict",
                        "action": "kept-source-dob"
                    })

                    conflict_count += 1

                # Preserve year-only records.
                if source_precision == "year":

                    year_precision += 1

                    year_only.append({
                        "id": player.get("id"),
                        "name": name,
                        "dateOfBirth": source_date,
                        "dateOfBirthPrecision": "year",
                        "matchMethod": None,
                        "wikidataId": None
                    })

                enriched.append(
                    original
                )

                continue

            # ------------------------------------------------
            # SAFE MATCH
            # ------------------------------------------------

            method = determine_match_method(
                source_precision,
                best["wikidataPrecision"],
                source_date,
                best["wikidataDate"]
            )

            if not method:

                enriched.append(
                    original
                )

                continue

            matched += 1

            result = dict(
                player
            )

            result["wikidataId"] = best[
                "qid"
            ]

            result["wikidataDateOfBirth"] = best[
                "wikidataDate"
            ]

            result[
                "wikidataDateOfBirthPrecision"
            ] = best[
                "wikidataPrecision"
            ]

            result["wikidataMatchScore"] = best[
                "score"
            ]

            result["wikidataMatchMethod"] = method

            # ------------------------------------------------
            # DOB UPGRADE
            # ------------------------------------------------

            if (
                source_precision == "year"
                and best["wikidataPrecision"] == "day"
            ):

                source_year = get_year(
                    source_date
                )

                wikidata_year = get_year(
                    best["wikidataDate"]
                )

                # FINAL SAFETY CHECK
                if source_year != wikidata_year:

                    conflicts.append({
                        "id": player.get("id"),
                        "name": name,
                        "sourceDateOfBirth": source_date,
                        "sourceDatePrecision": source_precision,
                        "sourceBirthYear": source_year,
                        "candidateDateOfBirth": best[
                            "wikidataDate"
                        ],
                        "candidateDatePrecision": best[
                            "wikidataPrecision"
                        ],
                        "candidateBirthYear": wikidata_year,
                        "matchedName": best[
                            "label"
                        ],
                        "wikidataId": best[
                            "qid"
                        ],
                        "nameScore": best[
                            "nameScore"
                        ],
                        "matchScore": best[
                            "score"
                        ],
                        "matchMethod": "year-conflict",
                        "action": "kept-source-dob"
                    })

                    conflict_count += 1

                    enriched.append(
                        original
                    )

                    continue

                # SAFE CORRECTION
                result["dateOfBirth"] = best[
                    "wikidataDate"
                ]

                result[
                    "dateOfBirthPrecision"
                ] = "day"

                corrections.append({
                    "id": player.get("id"),
                    "name": name,
                    "sourceDateOfBirth": source_date,
                    "sourceDatePrecision": source_precision,
                    "correctedDateOfBirth": best[
                        "wikidataDate"
                    ],
                    "correctedDatePrecision": "day",
                    "matchMethod": method,
                    "matchScore": best[
                        "score"
                    ],
                    "wikidataId": best[
                        "qid"
                    ]
                })

                corrected += 1
                full_dob += 1

            elif (
                source_precision == "year"
                and best["wikidataPrecision"] == "month"
            ):

                source_year = get_year(
                    source_date
                )

                wikidata_year = get_year(
                    best["wikidataDate"]
                )

                if source_year == wikidata_year:

                    result["dateOfBirth"] = best[
                        "wikidataDate"
                    ]

                    result[
                        "dateOfBirthPrecision"
                    ] = "month"

                    month_precision += 1

            elif (
                source_precision == "year"
                and best["wikidataPrecision"] == "year"
            ):

                year_precision += 1

                year_only.append({
                    "id": player.get("id"),
                    "name": name,
                    "dateOfBirth": source_date,
                    "dateOfBirthPrecision": "year",
                    "matchMethod": "year-only",
                    "wikidataId": best["qid"]
                })

            # ------------------------------------------------
            # DECEASED
            # ------------------------------------------------

            if best["deathDate"]:

                deceased += 1

                result[
                    "wikidataDateOfDeath"
                ] = best[
                    "deathDate"
                ]

            # ------------------------------------------------
            # MATCH AUDIT
            # ------------------------------------------------

            matches.append({
                "id": player.get("id"),
                "name": name,
                "wikidataId": best["qid"],
                "matchedName": best["label"],
                "wikidataDescription": best[
                    "description"
                ],
                "sourceDateOfBirth": source_date,
                "sourceDatePrecision": source_precision,
                "wikidataDateOfBirth": best[
                    "wikidataDate"
                ],
                "wikidataDateOfBirthPrecision": best[
                    "wikidataPrecision"
                ],
                "matchMethod": method,
                "nameScore": best[
                    "nameScore"
                ],
                "matchScore": best[
                    "score"
                ],
                "yearMatch": best[
                    "yearMatch"
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

    ENRICHED_FILE.parent.mkdir(
        parents=True,
        exist_ok=True
    )

    with open(
        ENRICHED_FILE,
        "w",
        encoding="utf-8"
    ) as f:

        json.dump(
            enriched,
            f,
            ensure_ascii=False,
            indent=2
        )

    with open(
        MATCHES_FILE,
        "w",
        encoding="utf-8"
    ) as f:

        json.dump(
            matches,
            f,
            ensure_ascii=False,
            indent=2
        )

    with open(
        CORRECTIONS_FILE,
        "w",
        encoding="utf-8"
    ) as f:

        json.dump(
            corrections,
            f,
            ensure_ascii=False,
            indent=2
        )

    with open(
        CONFLICTS_FILE,
        "w",
        encoding="utf-8"
    ) as f:

        json.dump(
            conflicts,
            f,
            ensure_ascii=False,
            indent=2
        )

    with open(
        YEAR_ONLY_FILE,
        "w",
        encoding="utf-8"
    ) as f:

        json.dump(
            year_only,
            f,
            ensure_ascii=False,
            indent=2
        )

    # ========================================================
    # FINAL REPORT
    # ========================================================

    print()
    print("=" * 60)
    print(
        "V2.2.1 TEST COMPLETE"
    )
    print("=" * 60)

    print(
        f"Players tested:        {len(test_players)}"
    )

    print(
        f"Matched:               {matched}"
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

    print(
        f"Errors:                {errors}"
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
