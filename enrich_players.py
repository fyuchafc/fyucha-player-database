import argparse
import csv
import json
import os
import re
import sys
import time
import unicodedata
from datetime import datetime
from difflib import SequenceMatcher
from pathlib import Path

import requests


# ============================================================
# FYUCHA PLAYER DATABASE
# GITHUB BASELINE WIKIDATA ENRICHMENT
#
# Version: BASELINE-GITHUB-1.0
#
# Purpose:
#   Enrich football player records using Wikidata.
#
# Features:
#   - Wikidata search
#   - Name matching
#   - Date-of-birth matching
#   - Football occupation detection
#   - Conservative candidate selection
#   - Diagnostics
#   - Checkpoints
#   - Resume support
#   - GitHub Actions compatible
#
# No API key required.
# ============================================================


# ============================================================
# CONFIGURATION
# ============================================================

WIKIDATA_API = "https://www.wikidata.org/w/api.php"

DEFAULT_INPUT = "players.json"
DEFAULT_OUTPUT = "enriched_players.json"
DEFAULT_CHECKPOINT = "enrichment_checkpoint.json"
DEFAULT_DIAGNOSTICS = "enrichment_diagnostics.csv"

CHECKPOINT_EVERY = 100

REQUEST_DELAY = 0.25

SEARCH_LIMIT = 10
MAX_CANDIDATES = 10

MIN_NAME_SCORE = 0.72
STRONG_NAME_SCORE = 0.90

FOOTBALL_TERMS = {
    "footballer",
    "football player",
    "soccer player",
    "association football player",
    "football manager",
    "football coach",
    "soccer",
    "football",
    "professional footballer",
    "professional football player",
    "former footballer",
    "former football player",
    "midfielder",
    "defender",
    "forward",
    "goalkeeper",
    "striker",
    "winger",
}


# Common football-related Wikidata occupations.
FOOTBALL_OCCUPATION_QIDS = {
    "Q937857",
    "Q10833314",
    "Q461057",
    "Q628099",
}


# ============================================================
# HTTP SESSION
# ============================================================

session = requests.Session()

session.headers.update(
    {
        "User-Agent": (
            "FyuchaPlayerDatabase/1.0 "
            "(football player database enrichment)"
        ),
        "Accept": "application/json",
    }
)


# ============================================================
# TEXT NORMALIZATION
# ============================================================

def normalize_text(value):
    if value is None:
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

    value = value.replace("&", " and ")

    value = re.sub(
        r"[^\w\s]",
        " ",
        value
    )

    value = re.sub(
        r"\s+",
        " ",
        value
    )

    return value.strip()


def tokenize(value):
    normalized = normalize_text(value)

    if not normalized:
        return set()

    return set(
        normalized.split()
    )


def name_similarity(a, b):
    a = normalize_text(a)
    b = normalize_text(b)

    if not a or not b:
        return 0.0

    if a == b:
        return 1.0

    sequence_score = SequenceMatcher(
        None,
        a,
        b
    ).ratio()

    a_tokens = tokenize(a)
    b_tokens = tokenize(b)

    if a_tokens and b_tokens:

        intersection = len(
            a_tokens & b_tokens
        )

        union = len(
            a_tokens | b_tokens
        )

        token_score = (
            intersection / union
            if union
            else 0.0
        )

    else:
        token_score = 0.0

    return round(
        sequence_score * 0.55
        + token_score * 0.45,
        4
    )


# ============================================================
# DATE NORMALIZATION
# ============================================================

def normalize_date(value):

    if not value:
        return None

    value = str(value).strip()

    match = re.search(
        r"(\d{4})-(\d{2})-(\d{2})",
        value
    )

    if not match:
        return None

    return (
        f"{match.group(1)}-"
        f"{match.group(2)}-"
        f"{match.group(3)}"
    )


# ============================================================
# WIKIDATA REQUEST
# ============================================================

def wikidata_request(params, retries=6):

    last_error = None

    for attempt in range(
        1,
        retries + 1
    ):

        try:

            response = session.get(
                WIKIDATA_API,
                params=params,
                timeout=45
            )

            if response.status_code == 200:
                return response.json()

            if response.status_code == 429:

                wait_time = min(
                    10 * attempt,
                    60
                )

                print(
                    f"  Rate limited. "
                    f"Waiting {wait_time}s..."
                )

                time.sleep(wait_time)

                continue

            if response.status_code >= 500:

                wait_time = min(
                    5 * attempt,
                    30
                )

                print(
                    f"  Wikidata server error "
                    f"{response.status_code}. "
                    f"Waiting {wait_time}s..."
                )

                time.sleep(wait_time)

                continue

            response.raise_for_status()

        except Exception as exc:

            last_error = exc

            wait_time = min(
                3 * attempt,
                30
            )

            print(
                f"  Request error "
                f"(attempt {attempt}/{retries}): "
                f"{exc}"
            )

            time.sleep(wait_time)

    raise RuntimeError(
        "Wikidata request failed after "
        f"{retries} attempts: {last_error}"
    )


# ============================================================
# SEARCH WIKIDATA
# ============================================================

def search_wikidata(player_name):

    params = {
        "action": "wbsearchentities",
        "search": player_name,
        "language": "en",
        "uselang": "en",
        "format": "json",
        "formatversion": "2",
        "limit": SEARCH_LIMIT,
        "type": "item",
    }

    data = wikidata_request(
        params
    )

    time.sleep(
        REQUEST_DELAY
    )

    return data.get(
        "search",
        []
    )


# ============================================================
# GET WIKIDATA ENTITIES
# ============================================================

def get_entities(qids):

    if not qids:
        return {}

    qids = list(
        dict.fromkeys(qids)
    )

    params = {
        "action": "wbgetentities",
        "ids": "|".join(qids),
        "props": (
            "labels|descriptions|"
            "aliases|claims|sitelinks"
        ),
        "languages": "en",
        "languagefallback": "1",
        "sitefilter": "enwiki",
        "format": "json",
        "formatversion": "2",
    }

    data = wikidata_request(
        params
    )

    time.sleep(
        REQUEST_DELAY
    )

    return data.get(
        "entities",
        {}
    )


# ============================================================
# ENTITY HELPERS
# ============================================================

def extract_label(
    entity,
    fallback=""
):

    label = entity.get(
        "labels",
        {}
    ).get("en")

    if isinstance(
        label,
        dict
    ):
        return label.get(
            "value",
            fallback
        )

    return fallback


def extract_description(entity):

    description = entity.get(
        "descriptions",
        {}
    ).get("en")

    if isinstance(
        description,
        dict
    ):
        return description.get(
            "value",
            ""
        )

    return ""


def extract_date_of_birth(entity):

    claims = entity.get(
        "claims",
        {}
    )

    dob_claims = claims.get(
        "P569",
        []
    )

    for claim in dob_claims:

        try:

            value = (
                claim
                .get("mainsnak", {})
                .get("datavalue", {})
                .get("value", {})
            )

            date = normalize_date(
                value.get("time")
            )

            if date:
                return date

        except Exception:
            continue

    return None


def extract_occupation_qids(entity):

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

        try:

            value = (
                claim
                .get("mainsnak", {})
                .get("datavalue", {})
                .get("value", {})
            )

            qid = value.get("id")

            if qid:
                qids.append(qid)

        except Exception:
            continue

    return qids


# ============================================================
# FOOTBALL SIGNAL
# ============================================================

def football_signal(entity):

    description = normalize_text(
        extract_description(entity)
    )

    occupations = set(
        extract_occupation_qids(entity)
    )

    if occupations & FOOTBALL_OCCUPATION_QIDS:
        return 1.0

    for term in FOOTBALL_TERMS:

        if term in description:
            return 0.8

    description_tokens = tokenize(
        description
    )

    if description_tokens & {
        "football",
        "soccer",
        "footballer",
        "midfielder",
        "defender",
        "forward",
        "goalkeeper",
        "striker",
        "winger",
    }:
        return 0.6

    return 0.0


# ============================================================
# SCORE CANDIDATE
# ============================================================

def score_candidate(
    player_name,
    source_dob,
    search_result,
    entity
):

    qid = search_result.get(
        "id",
        ""
    )

    matched_name = extract_label(
        entity,
        search_result.get(
            "label",
            ""
        )
    )

    description = extract_description(
        entity
    )

    wikidata_dob = extract_date_of_birth(
        entity
    )

    name_score = name_similarity(
        player_name,
        matched_name
    )

    football_score = football_signal(
        entity
    )

    dob_match = False

    if source_dob and wikidata_dob:

        dob_match = (
            normalize_date(source_dob)
            == normalize_date(wikidata_dob)
        )

    score = (
        name_score * 70
        + football_score * 20
    )

    if dob_match:
        score += 100

    return {
        "qid": qid,
        "matchedName": matched_name,
        "wikidataDateOfBirth": wikidata_dob,
        "description": description,
        "nameScore": round(
            name_score,
            4
        ),
        "footballScore": round(
            football_score,
            4
        ),
        "dobMatch": dob_match,
        "score": round(
            score,
            4
        ),
    }


# ============================================================
# ACCEPTANCE RULES
# ============================================================

def classify_candidate(candidate):

    name_score = candidate[
        "nameScore"
    ]

    football_score = candidate[
        "footballScore"
    ]

    dob_match = candidate[
        "dobMatch"
    ]

    # Exact DOB + reasonable name.
    if (
        dob_match
        and name_score >= 0.72
    ):
        return (
            True,
            "exact_name_dob"
        )

    # Very strong name + football evidence.
    if (
        name_score >= 0.90
        and football_score >= 0.6
    ):
        return (
            True,
            "strong_name_football"
        )

    # Extremely strong name.
    if name_score >= 0.96:
        return (
            True,
            "very_strong_name"
        )

    # Strong name + football evidence.
    if (
        name_score >= 0.85
        and football_score >= 0.6
    ):
        return (
            True,
            "strong_name_football"
        )

    return (
        False,
        "below_threshold"
    )


# ============================================================
# ENRICH ONE PLAYER
# ============================================================

def enrich_player(
    player,
    index
):

    player_name = (
        player.get("playerName")
        or player.get("name")
        or player.get("fullName")
        or ""
    )

    player_name = str(
        player_name
    ).strip()

    source_dob = (
        player.get("sourceDateOfBirth")
        or player.get("dateOfBirth")
        or player.get("dob")
        or ""
    )

    source_dob = normalize_date(
        source_dob
    )

    result = dict(
        player
    )

    checked_at = (
        datetime.utcnow()
        .isoformat()
        + "Z"
    )

    if not player_name:

        result.update(
            {
                "wikidataId": None,
                "matchedName": None,
                "wikidataDateOfBirth": None,
                "enrichmentStatus": "unmatched",
                "matchMethod": "missing_name",
                "matchScore": 0,
                "enrichmentCheckedAt": checked_at,
            }
        )

        return result, {
            "index": index,
            "playerName": "",
            "status": "unmatched",
            "reason": "missing_name",
            "wikidataId": "",
            "matchedName": "",
            "sourceDateOfBirth": source_dob or "",
            "wikidataDateOfBirth": "",
            "score": 0,
            "nameScore": 0,
            "footballScore": 0,
            "dobMatch": False,
        }

    # --------------------------------------------------------
    # SEARCH
    # --------------------------------------------------------

    search_results = search_wikidata(
        player_name
    )

    if not search_results:

        result.update(
            {
                "wikidataId": None,
                "matchedName": None,
                "wikidataDateOfBirth": None,
                "enrichmentStatus": "unmatched",
                "matchMethod": "no_search_results",
                "matchScore": 0,
                "enrichmentCheckedAt": checked_at,
            }
        )

        return result, {
            "index": index,
            "playerName": player_name,
            "status": "unmatched",
            "reason": "no_search_results",
            "wikidataId": "",
            "matchedName": "",
            "sourceDateOfBirth": source_dob or "",
            "wikidataDateOfBirth": "",
            "score": 0,
            "nameScore": 0,
            "footballScore": 0,
            "dobMatch": False,
        }

    # --------------------------------------------------------
    # FETCH CANDIDATES
    # --------------------------------------------------------

    candidate_results = (
        search_results[
            :MAX_CANDIDATES
        ]
    )

    qids = [
        item.get("id")
        for item in candidate_results
        if item.get("id")
    ]

    entities = get_entities(
        qids
    )

    # --------------------------------------------------------
    # SCORE
    # --------------------------------------------------------

    candidates = []

    for search_result in candidate_results:

        qid = search_result.get(
            "id"
        )

        if not qid:
            continue

        entity = entities.get(
            qid
        )

        if not entity:
            continue

        candidate = score_candidate(
            player_name,
            source_dob,
            search_result,
            entity
        )

        candidates.append(
            candidate
        )

    if not candidates:

        result.update(
            {
                "wikidataId": None,
                "matchedName": None,
                "wikidataDateOfBirth": None,
                "enrichmentStatus": "unmatched",
                "matchMethod": "no_valid_candidates",
                "matchScore": 0,
                "enrichmentCheckedAt": checked_at,
            }
        )

        return result, {
            "index": index,
            "playerName": player_name,
            "status": "unmatched",
            "reason": "no_valid_candidates",
            "wikidataId": "",
            "matchedName": "",
            "sourceDateOfBirth": source_dob or "",
            "wikidataDateOfBirth": "",
            "score": 0,
            "nameScore": 0,
            "footballScore": 0,
            "dobMatch": False,
        }

    candidates.sort(
        key=lambda x: x["score"],
        reverse=True
    )

    best = candidates[0]

    accepted, method = classify_candidate(
        best
    )

    # --------------------------------------------------------
    # ACCEPTED
    # --------------------------------------------------------

    if accepted:

        result.update(
            {
                "wikidataId": best["qid"],
                "matchedName": best["matchedName"],
                "wikidataDateOfBirth": best[
                    "wikidataDateOfBirth"
                ],
                "wikidataDescription": best[
                    "description"
                ],
                "enrichmentStatus": "matched",
                "matchMethod": method,
                "matchScore": best["score"],
                "nameScore": best["nameScore"],
                "footballScore": best[
                    "footballScore"
                ],
                "dobMatch": best["dobMatch"],
                "enrichmentCheckedAt": checked_at,
            }
        )

        return result, {
            "index": index,
            "playerName": player_name,
            "status": "matched",
            "reason": method,
            "wikidataId": best["qid"],
            "matchedName": best["matchedName"],
            "sourceDateOfBirth": source_dob or "",
            "wikidataDateOfBirth": best[
                "wikidataDateOfBirth"
            ] or "",
            "score": best["score"],
            "nameScore": best["nameScore"],
            "footballScore": best[
                "footballScore"
            ],
            "dobMatch": best["dobMatch"],
        }

    # --------------------------------------------------------
    # REJECTED
    # --------------------------------------------------------

    result.update(
        {
            "wikidataId": None,
            "matchedName": None,
            "wikidataDateOfBirth": None,
            "wikidataDescription": None,
            "enrichmentStatus": "unmatched",
            "matchMethod": method,
            "matchScore": best["score"],
            "nameScore": best["nameScore"],
            "footballScore": best[
                "footballScore"
            ],
            "dobMatch": best["dobMatch"],
            "bestCandidateId": best["qid"],
            "bestCandidateName": best[
                "matchedName"
            ],
            "bestCandidateDob": best[
                "wikidataDateOfBirth"
            ],
            "bestCandidateDescription": best[
                "description"
            ],
            "enrichmentCheckedAt": checked_at,
        }
    )

    return result, {
        "index": index,
        "playerName": player_name,
        "status": "unmatched",
        "reason": method,
        "wikidataId": "",
        "matchedName": best[
            "matchedName"
        ],
        "sourceDateOfBirth": source_dob or "",
        "wikidataDateOfBirth": best[
            "wikidataDateOfBirth"
        ] or "",
        "score": best["score"],
        "nameScore": best["nameScore"],
        "footballScore": best[
            "footballScore"
        ],
        "dobMatch": best["dobMatch"],
    }


# ============================================================
# ATOMIC JSON SAVE
# ============================================================

def save_json(
    path,
    data
):

    path = Path(
        path
    )

    temp_path = Path(
        str(path) + ".tmp"
    )

    with open(
        temp_path,
        "w",
        encoding="utf-8"
    ) as f:

        json.dump(
            data,
            f,
            ensure_ascii=False,
            indent=2
        )

    os.replace(
        temp_path,
        path
    )


# ============================================================
# CHECKPOINT
# ============================================================

def save_checkpoint(
    path,
    results,
    diagnostics,
    next_index
):

    checkpoint = {
        "version": "baseline-github-1.0",
        "savedAt": (
            datetime.utcnow()
            .isoformat()
            + "Z"
        ),
        "nextIndex": next_index,
        "results": results,
        "diagnostics": diagnostics,
    }

    save_json(
        path,
        checkpoint
    )


def load_checkpoint(path):

    path = Path(
        path
    )

    if not path.exists():
        return None

    try:

        with open(
            path,
            "r",
            encoding="utf-8"
        ) as f:

            return json.load(f)

    except Exception as exc:

        print(
            "WARNING: checkpoint could "
            "not be loaded:"
        )

        print(exc)

        return None


# ============================================================
# DIAGNOSTICS
# ============================================================

def write_diagnostics(
    path,
    diagnostics
):

    fields = [
        "index",
        "playerName",
        "status",
        "reason",
        "wikidataId",
        "matchedName",
        "sourceDateOfBirth",
        "wikidataDateOfBirth",
        "score",
        "nameScore",
        "footballScore",
        "dobMatch",
    ]

    with open(
        path,
        "w",
        newline="",
        encoding="utf-8"
    ) as f:

        writer = csv.DictWriter(
            f,
            fieldnames=fields,
            extrasaction="ignore"
        )

        writer.writeheader()

        writer.writerows(
            diagnostics
        )


# ============================================================
# SUMMARY
# ============================================================

def print_summary(
    results,
    diagnostics
):

    total = len(
        results
    )

    matched = sum(
        1
        for item in diagnostics
        if item.get("status")
        == "matched"
    )

    unmatched = sum(
        1
        for item in diagnostics
        if item.get("status")
        == "unmatched"
    )

    errors = sum(
        1
        for item in diagnostics
        if item.get("status")
        == "error"
    )

    dob_matches = sum(
        1
        for item in diagnostics
        if item.get("dobMatch")
        is True
    )

    print()
    print(
        "=" * 70
    )

    print(
        "FYUCHA BASELINE ENRICHMENT SUMMARY"
    )

    print(
        "=" * 70
    )

    print(
        f"Processed:          {total:,}"
    )

    print(
        f"Matched:            {matched:,}"
    )

    print(
        f"Unmatched:          {unmatched:,}"
    )

    print(
        f"Errors:             {errors:,}"
    )

    print(
        f"Exact DOB matches:  {dob_matches:,}"
    )

    if total:

        print(
            f"Match rate:         "
            f"{matched / total * 100:.2f}%"
        )

    print(
        "=" * 70
    )


# ============================================================
# MAIN
# ============================================================

def main():

    parser = argparse.ArgumentParser()

    parser.add_argument(
        "--input",
        default=DEFAULT_INPUT
    )

    parser.add_argument(
        "--output",
        default=DEFAULT_OUTPUT
    )

    parser.add_argument(
        "--checkpoint",
        default=DEFAULT_CHECKPOINT
    )

    parser.add_argument(
        "--diagnostics",
        default=DEFAULT_DIAGNOSTICS
    )

    parser.add_argument(
        "--limit",
        type=int,
        default=0,
        help=(
            "Number of players to process. "
            "0 = all."
        )
    )

    parser.add_argument(
        "--resume",
        action="store_true"
    )

    parser.add_argument(
        "--fresh",
        action="store_true"
    )

    args = parser.parse_args()

    input_path = Path(
        args.input
    )

    output_path = Path(
        args.output
    )

    checkpoint_path = Path(
        args.checkpoint
    )

    diagnostics_path = Path(
        args.diagnostics
    )

    if not input_path.exists():

        print(
            f"ERROR: Input file not found: "
            f"{input_path}"
        )

        sys.exit(1)

    print()
    print(
        "=" * 70
    )

    print(
        "FYUCHA PLAYER DATABASE"
    )

    print(
        "GITHUB BASELINE WIKIDATA ENRICHMENT"
    )

    print(
        "=" * 70
    )

    print(
        f"Input:       {input_path}"
    )

    print(
        f"Output:      {output_path}"
    )

    print(
        f"Checkpoint:  {checkpoint_path}"
    )

    print(
        f"Diagnostics: {diagnostics_path}"
    )

    print()

    with open(
        input_path,
        "r",
        encoding="utf-8"
    ) as f:

        database = json.load(f)

    # --------------------------------------------------------
    # Support:
    #
    # [
    #   {...}
    # ]
    #
    # OR:
    #
    # {
    #   "players": [...]
    # }
    # --------------------------------------------------------

    if isinstance(
        database,
        list
    ):

        players = database
        wrapper = None

    elif isinstance(
        database,
        dict
    ):

        if isinstance(
            database.get("players"),
            list
        ):

            players = database[
                "players"
            ]

            wrapper = database

        else:

            print(
                "ERROR: JSON object does not "
                "contain a players list."
            )

            sys.exit(1)

    else:

        print(
            "ERROR: Unsupported JSON structure."
        )

        sys.exit(1)

    print(
        f"Players available: "
        f"{len(players):,}"
    )

    if args.limit > 0:

        target_total = min(
            args.limit,
            len(players)
        )

    else:

        target_total = len(
            players
        )

    print(
        f"Players to process: "
        f"{target_total:,}"
    )

    # --------------------------------------------------------
    # Checkpoint loading
    # --------------------------------------------------------

    results = []
    diagnostics = []
    start_index = 0

    if (
        args.resume
        and not args.fresh
    ):

        checkpoint = load_checkpoint(
            checkpoint_path
        )

        if checkpoint:

            results = checkpoint.get(
                "results",
                []
            )

            diagnostics = checkpoint.get(
                "diagnostics",
                []
            )

            start_index = int(
                checkpoint.get(
                    "nextIndex",
                    len(results)
                )
            )

            print()
            print(
                "RESUMING FROM CHECKPOINT"
            )

            print(
                f"Already processed: "
                f"{start_index:,}"
            )

        else:

            print(
                "No checkpoint found."
            )

    else:

        print(
            "Starting fresh."
        )

    # --------------------------------------------------------
    # Processing
    # --------------------------------------------------------

    for index in range(
        start_index,
        target_total
    ):

        player = players[
            index
        ]

        player_name = (
            player.get(
                "playerName"
            )
            or player.get(
                "name"
            )
            or player.get(
                "fullName"
            )
            or "UNKNOWN"
        )

        print(
            f"[{index + 1:,}/"
            f"{target_total:,}] "
            f"{player_name}",
            end=" ",
            flush=True
        )

        try:

            enriched, diagnostic = (
                enrich_player(
                    player,
                    index
                )
            )

            results.append(
                enriched
            )

            diagnostics.append(
                diagnostic
            )

            if (
                diagnostic["status"]
                == "matched"
            ):

                print(
                    "→ MATCH "
                    f"{diagnostic['wikidataId']} "
                    f"("
                    f"{diagnostic['reason']}"
                    ")"
                )

            else:

                print(
                    "→ UNMATCHED "
                    f"("
                    f"{diagnostic['reason']}"
                    ")"
                )

        except KeyboardInterrupt:

            print()
            print(
                "Interrupted."
            )

            print(
                "Saving checkpoint..."
            )

            save_checkpoint(
                checkpoint_path,
                results,
                diagnostics,
                index
            )

            write_diagnostics(
                diagnostics_path,
                diagnostics
            )

            raise

        except Exception as exc:

            print(
                f"→ ERROR: {exc}"
            )

            error_result = dict(
                player
            )

            error_result.update(
                {
                    "wikidataId": None,
                    "matchedName": None,
                    "wikidataDateOfBirth": None,
                    "enrichmentStatus": "error",
                    "matchMethod": "processing_error",
                    "matchScore": 0,
                    "enrichmentError": str(
                        exc
                    ),
                    "enrichmentCheckedAt": (
                        datetime.utcnow()
                        .isoformat()
                        + "Z"
                    ),
                }
            )

            results.append(
                error_result
            )

            diagnostics.append(
                {
                    "index": index,
                    "playerName": str(
                        player_name
                    ),
                    "status": "error",
                    "reason": "processing_error",
                    "wikidataId": "",
                    "matchedName": "",
                    "sourceDateOfBirth": "",
                    "wikidataDateOfBirth": "",
                    "score": 0,
                    "nameScore": 0,
                    "footballScore": 0,
                    "dobMatch": False,
                }
            )

        # ----------------------------------------------------
        # Checkpoint every 100 players.
        # ----------------------------------------------------

        processed = index + 1

        if (
            processed % CHECKPOINT_EVERY == 0
            or processed == target_total
        ):

            print()
            print(
                f"Saving checkpoint "
                f"({processed:,})..."
            )

            save_checkpoint(
                checkpoint_path,
                results,
                diagnostics,
                processed
            )

            write_diagnostics(
                diagnostics_path,
                diagnostics
            )

            print(
                "Checkpoint saved."
            )

    # --------------------------------------------------------
    # FINAL DATABASE
    # --------------------------------------------------------

    if wrapper is not None:

        final_database = dict(
            wrapper
        )

        final_database[
            "players"
        ] = results

    else:

        final_database = results

    save_json(
        output_path,
        final_database
    )

    write_diagnostics(
        diagnostics_path,
        diagnostics
    )

    print()
    print(
        "FINAL DATABASE CREATED:"
    )

    print(
        output_path.resolve()
    )

    print_summary(
        results,
        diagnostics
    )

    print()
    print(
        "DIAGNOSTICS CREATED:"
    )

    print(
        diagnostics_path.resolve()
    )

    print()
    print(
        "ENRICHMENT COMPLETE."
    )


if __name__ == "__main__":
    main()
