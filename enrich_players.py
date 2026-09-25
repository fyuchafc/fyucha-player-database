# ============================================================
# FYUCHA PLAYER DATABASE
# BASELINE WIKIDATA ENRICHMENT
#
# Version: BASELINE 1.0
#
# Purpose:
#   Enrich football player records with Wikidata IDs and DOB.
#
# Design:
#   - Simple
#   - Conservative
#   - Resumable
#   - Transparent diagnostics
#   - No external API keys
#   - No SPARQL dependency
#
# Wikidata API:
#   https://www.wikidata.org/w/api.php
#
# Main API operations:
#   wbsearchentities
#   wbgetentities
#
# ============================================================

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
# CONFIGURATION
# ============================================================

DEFAULT_INPUT = "players.json"
DEFAULT_OUTPUT = "enriched_players.json"
DEFAULT_CHECKPOINT = "enrichment_checkpoint.json"
DEFAULT_DIAGNOSTICS = "enrichment_diagnostics.csv"

CHECKPOINT_EVERY = 100

# Delay between Wikidata API requests.
# Keep this conservative.
REQUEST_DELAY = 0.25

# Number of search candidates to request.
SEARCH_LIMIT = 10

# Maximum candidates to inspect in detail.
MAX_CANDIDATES = 10

# Minimum name similarity for a potential match.
MIN_NAME_SCORE = 0.72

# Strong name score.
STRONG_NAME_SCORE = 0.90

# If DOB matches exactly, this is considered very strong.
DOB_EXACT_BONUS = 100

# Football-related terms used in descriptions/occupations.
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

# Occupation QIDs frequently associated with football.
FOOTBALL_OCCUPATION_QIDS = {
    "Q937857",     # association football player
    "Q10833314",   # football player
    "Q461057",     # football manager
    "Q628099",     # coach
}

# ============================================================
# SESSION
# ============================================================

session = requests.Session()

session.headers.update(
    {
        "User-Agent": (
            "FyuchaPlayerDatabase/1.0 "
            "(football player database enrichment; "
            "contact via Wikimedia API)"
        ),
        "Accept": "application/json",
    }
)

WIKIDATA_API = "https://www.wikidata.org/w/api.php"


# ============================================================
# GENERAL HELPERS
# ============================================================

def normalize_text(value):
    """
    Normalize text for comparison.

    Examples:
        José Mourinho -> jose mourinho
        Kylian Mbappé -> kylian mbappe
        Ronaldinho Gaúcho -> ronaldinho gaucho
    """

    if value is None:
        return ""

    value = str(value)

    value = unicodedata.normalize("NFKD", value)

    value = "".join(
        char
        for char in value
        if not unicodedata.combining(char)
    )

    value = value.lower()

    value = value.replace("&", " and ")

    value = re.sub(r"[^\w\s]", " ", value)

    value = re.sub(r"\s+", " ", value).strip()

    return value


def tokenize(value):
    normalized = normalize_text(value)

    if not normalized:
        return set()

    return set(normalized.split())


def name_similarity(a, b):
    """
    Return a 0-1 similarity score.
    """

    a_norm = normalize_text(a)
    b_norm = normalize_text(b)

    if not a_norm or not b_norm:
        return 0.0

    if a_norm == b_norm:
        return 1.0

    sequence_score = SequenceMatcher(
        None,
        a_norm,
        b_norm,
    ).ratio()

    a_tokens = tokenize(a)
    b_tokens = tokenize(b)

    if a_tokens and b_tokens:
        intersection = len(a_tokens & b_tokens)
        union = len(a_tokens | b_tokens)

        jaccard = intersection / union if union else 0.0
    else:
        jaccard = 0.0

    # Give token similarity significant importance.
    score = (
        sequence_score * 0.55
        + jaccard * 0.45
    )

    return round(score, 4)


def normalize_date(value):
    """
    Convert DOB values into YYYY-MM-DD when possible.

    Accepts:
        1987-06-15
        1987-06-15T00:00:00Z
        +1987-06-15T00:00:00Z
    """

    if not value:
        return None

    value = str(value).strip()

    match = re.search(
        r"(\d{4})-(\d{2})-(\d{2})",
        value,
    )

    if not match:
        return None

    return (
        f"{match.group(1)}-"
        f"{match.group(2)}-"
        f"{match.group(3)}"
    )


def safe_int(value, default=0):
    try:
        return int(value)
    except Exception:
        return default


# ============================================================
# WIKIDATA REQUEST
# ============================================================

def wikidata_request(params, retries=5):
    """
    Perform a Wikidata API request with retry handling.
    """

    last_error = None

    for attempt in range(1, retries + 1):

        try:

            response = session.get(
                WIKIDATA_API,
                params=params,
                timeout=45,
            )

            if response.status_code == 200:

                return response.json()

            # Rate limited.
            if response.status_code == 429:

                wait_time = min(
                    10 * attempt,
                    60,
                )

                print(
                    f"\nWikidata rate limit (429). "
                    f"Waiting {wait_time}s..."
                )

                time.sleep(wait_time)

                continue

            # Server errors.
            if response.status_code >= 500:

                wait_time = min(
                    5 * attempt,
                    30,
                )

                print(
                    f"\nWikidata server error "
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
                30,
            )

            print(
                f"\nRequest error "
                f"(attempt {attempt}/{retries}): "
                f"{exc}"
            )

            time.sleep(wait_time)

    raise RuntimeError(
        f"Wikidata request failed after "
        f"{retries} attempts: {last_error}"
    )


# ============================================================
# WIKIDATA SEARCH
# ============================================================

def search_wikidata(player_name):
    """
    Search Wikidata for a player name.
    """

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

    data = wikidata_request(params)

    time.sleep(REQUEST_DELAY)

    return data.get("search", [])


# ============================================================
# WIKIDATA ENTITY FETCH
# ============================================================

def get_entities(qids):
    """
    Retrieve multiple Wikidata entities in one request.

    Wikidata supports multiple entity IDs in wbgetentities.
    """

    if not qids:
        return {}

    # Remove duplicates.
    qids = list(dict.fromkeys(qids))

    params = {
        "action": "wbgetentities",
        "ids": "|".join(qids),
        "props": "labels|descriptions|aliases|claims|sitelinks",
        "languages": "en",
        "languagefallback": "1",
        "sitefilter": "enwiki",
        "format": "json",
        "formatversion": "2",
    }

    data = wikidata_request(params)

    time.sleep(REQUEST_DELAY)

    return data.get("entities", {})


# ============================================================
# EXTRACT DOB
# ============================================================

def extract_date_of_birth(entity):
    """
    Extract P569 date of birth from a Wikidata entity.
    """

    claims = entity.get("claims", {})

    dob_claims = claims.get("P569", [])

    dates = []

    for claim in dob_claims:

        try:

            mainsnak = claim.get("mainsnak", {})

            datavalue = mainsnak.get("datavalue", {})

            value = datavalue.get("value", {})

            time_value = value.get("time")

            if time_value:

                date = normalize_date(time_value)

                if date:
                    dates.append(date)

        except Exception:
            continue

    if dates:
        return dates[0]

    return None


# ============================================================
# EXTRACT OCCUPATION QIDS
# ============================================================

def extract_occupation_qids(entity):

    claims = entity.get("claims", {})

    occupation_claims = claims.get("P106", [])

    qids = []

    for claim in occupation_claims:

        try:

            mainsnak = claim.get("mainsnak", {})

            datavalue = mainsnak.get("datavalue", {})

            value = datavalue.get("value", {})

            qid = value.get("id")

            if qid:
                qids.append(qid)

        except Exception:
            continue

    return qids


# ============================================================
# EXTRACT DESCRIPTION
# ============================================================

def extract_description(entity):

    descriptions = entity.get(
        "descriptions",
        {},
    )

    description = descriptions.get("en")

    if isinstance(description, dict):
        return description.get("value", "")

    return ""


# ============================================================
# EXTRACT LABEL
# ============================================================

def extract_label(entity, fallback=""):

    labels = entity.get(
        "labels",
        {},
    )

    label = labels.get("en")

    if isinstance(label, dict):
        return label.get("value", fallback)

    return fallback


# ============================================================
# FOOTBALL DETECTION
# ============================================================

def football_signal(entity):

    description = normalize_text(
        extract_description(entity)
    )

    occupation_qids = set(
        extract_occupation_qids(entity)
    )

    # Strong structured occupation signal.
    if occupation_qids & FOOTBALL_OCCUPATION_QIDS:
        return 1.0

    # Description signal.
    for term in FOOTBALL_TERMS:

        if term in description:
            return 0.8

    # Token-based fallback.
    description_tokens = tokenize(description)

    football_tokens = {
        "football",
        "soccer",
        "footballer",
        "midfielder",
        "defender",
        "forward",
        "goalkeeper",
        "striker",
        "winger",
    }

    if description_tokens & football_tokens:
        return 0.6

    return 0.0


# ============================================================
# CANDIDATE SCORING
# ============================================================

def score_candidate(
    player_name,
    source_dob,
    search_result,
    entity,
):
    """
    Score a Wikidata candidate.

    Scoring is intentionally conservative.

    Components:
        - name similarity
        - exact DOB
        - football signal
    """

    qid = search_result.get(
        "id",
        "",
    )

    matched_name = extract_label(
        entity,
        search_result.get(
            "label",
            "",
        ),
    )

    description = extract_description(
        entity
    )

    wikidata_dob = extract_date_of_birth(
        entity
    )

    name_score = name_similarity(
        player_name,
        matched_name,
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
        score += DOB_EXACT_BONUS

    return {
        "qid": qid,
        "matchedName": matched_name,
        "wikidataDateOfBirth": wikidata_dob,
        "description": description,
        "nameScore": round(name_score, 4),
        "footballScore": round(
            football_score,
            4,
        ),
        "dobMatch": dob_match,
        "score": round(score, 4),
    }


# ============================================================
# ACCEPT / REJECT LOGIC
# ============================================================

def classify_candidate(candidate, source_dob):

    name_score = candidate["nameScore"]

    dob_match = candidate["dobMatch"]

    football_score = candidate[
        "footballScore"
    ]

    # --------------------------------------------------------
    # Exact DOB + reasonable name = very strong.
    # --------------------------------------------------------

    if dob_match and name_score >= 0.72:

        return (
            True,
            "exact_name_dob"
        )

    # --------------------------------------------------------
    # Very strong name + football signal.
    # --------------------------------------------------------

    if (
        name_score >= STRONG_NAME_SCORE
        and football_score >= 0.6
    ):

        return (
            True,
            "strong_name_football"
        )

    # --------------------------------------------------------
    # Very strong name without DOB.
    #
    # This is intentionally allowed because many Wikidata
    # records can have missing DOB data.
    # --------------------------------------------------------

    if name_score >= 0.96:

        return (
            True,
            "very_strong_name"
        )

    # --------------------------------------------------------
    # Moderate name + football + DOB unavailable.
    #
    # Require a fairly high combined score.
    # --------------------------------------------------------

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
# PROCESS ONE PLAYER
# ============================================================

def enrich_player(player, index, total):

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

    result = dict(player)

    # --------------------------------------------------------
    # Empty name
    # --------------------------------------------------------

    if not player_name:

        result.update(
            {
                "wikidataId": None,
                "matchedName": None,
                "wikidataDateOfBirth": None,
                "enrichmentStatus": "unmatched",
                "matchMethod": "missing_name",
                "matchScore": 0,
                "enrichmentCheckedAt": datetime.utcnow().isoformat()
                + "Z",
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
        }

    # --------------------------------------------------------
    # Search
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
                "enrichmentCheckedAt": datetime.utcnow().isoformat()
                + "Z",
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
        }

    # --------------------------------------------------------
    # Candidate IDs
    # --------------------------------------------------------

    candidate_search_results = (
        search_results[
            :MAX_CANDIDATES
        ]
    )

    qids = [
        item.get("id")
        for item in candidate_search_results
        if item.get("id")
    ]

    entities = get_entities(qids)

    # --------------------------------------------------------
    # Score candidates
    # --------------------------------------------------------

    candidates = []

    for search_result in candidate_search_results:

        qid = search_result.get(
            "id"
        )

        if not qid:
            continue

        entity = entities.get(qid)

        if not entity:
            continue

        candidate = score_candidate(
            player_name,
            source_dob,
            search_result,
            entity,
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
                "enrichmentCheckedAt": datetime.utcnow().isoformat()
                + "Z",
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
        }

    # Highest score first.
    candidates.sort(
        key=lambda x: x["score"],
        reverse=True,
    )

    best = candidates[0]

    accepted, method = classify_candidate(
        best,
        source_dob,
    )

    # --------------------------------------------------------
    # MATCH
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
                "enrichmentCheckedAt": datetime.utcnow().isoformat()
                + "Z",
            }
        )

        diagnostic = {
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

        return result, diagnostic

    # --------------------------------------------------------
    # UNMATCHED
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
            "enrichmentCheckedAt": datetime.utcnow().isoformat()
            + "Z",
        }
    )

    diagnostic = {
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

    return result, diagnostic


# ============================================================
# CHECKPOINT FUNCTIONS
# ============================================================

def save_json(path, data):

    temp_path = Path(
        str(path) + ".tmp"
    )

    with open(
        temp_path,
        "w",
        encoding="utf-8",
    ) as f:

        json.dump(
            data,
            f,
            ensure_ascii=False,
            indent=2,
        )

    os.replace(
        temp_path,
        path,
    )


def save_checkpoint(
    checkpoint_path,
    results,
    diagnostics,
    next_index,
):

    checkpoint = {
        "version": "baseline-1.0",
        "savedAt": datetime.utcnow().isoformat()
        + "Z",
        "nextIndex": next_index,
        "results": results,
        "diagnostics": diagnostics,
    }

    save_json(
        checkpoint_path,
        checkpoint,
    )


def load_checkpoint(
    checkpoint_path
):

    path = Path(
        checkpoint_path
    )

    if not path.exists():
        return None

    try:

        with open(
            path,
            "r",
            encoding="utf-8",
        ) as f:

            return json.load(f)

    except Exception as exc:

        print(
            "WARNING: Could not load checkpoint:"
        )

        print(exc)

        return None


# ============================================================
# DIAGNOSTICS CSV
# ============================================================

def write_diagnostics(
    path,
    diagnostics,
):

    if not diagnostics:
        return

    fieldnames = [
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
        encoding="utf-8",
    ) as f:

        writer = csv.DictWriter(
            f,
            fieldnames=fieldnames,
            extrasaction="ignore",
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
    diagnostics,
):

    total = len(results)

    matched = sum(
        1
        for row in diagnostics
        if row.get("status")
        == "matched"
    )

    unmatched = total - matched

    exact_dob = sum(
        1
        for row in diagnostics
        if row.get("dobMatch")
        is True
    )

    print()
    print("=" * 70)
    print("FYUCHA BASELINE ENRICHMENT SUMMARY")
    print("=" * 70)

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
        f"Exact DOB matches:  {exact_dob:,}"
    )

    if total:

        print(
            f"Match rate:         "
            f"{matched / total * 100:.2f}%"
        )

    print("=" * 70)


# ============================================================
# MAIN
# ============================================================

def main():

    parser = argparse.ArgumentParser(
        description=(
            "Fyucha Player Database "
            "Wikidata Baseline Enrichment"
        )
    )

    parser.add_argument(
        "--input",
        default=DEFAULT_INPUT,
        help="Input player JSON",
    )

    parser.add_argument(
        "--output",
        default=DEFAULT_OUTPUT,
        help="Output enriched JSON",
    )

    parser.add_argument(
        "--checkpoint",
        default=DEFAULT_CHECKPOINT,
        help="Checkpoint JSON",
    )

    parser.add_argument(
        "--diagnostics",
        default=DEFAULT_DIAGNOSTICS,
        help="Diagnostics CSV",
    )

    parser.add_argument(
        "--limit",
        type=int,
        default=0,
        help=(
            "Process only N players. "
            "0 means all players."
        ),
    )

    parser.add_argument(
        "--resume",
        action="store_true",
        help="Resume from checkpoint",
    )

    parser.add_argument(
        "--fresh",
        action="store_true",
        help="Ignore existing checkpoint",
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

    # --------------------------------------------------------
    # Validate input.
    # --------------------------------------------------------

    if not input_path.exists():

        print(
            f"ERROR: Input file not found: "
            f"{input_path}"
        )

        sys.exit(1)

    # --------------------------------------------------------
    # Load players.
    # --------------------------------------------------------

    print()
    print("=" * 70)
    print("FYUCHA PLAYER DATABASE")
    print("WIKIDATA BASELINE ENRICHMENT")
    print("=" * 70)

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
        encoding="utf-8",
    ) as f:

        database = json.load(f)

    # --------------------------------------------------------
    # Support either:
    #
    # [
    #   {...},
    #   {...}
    # ]
    #
    # OR
    #
    # {
    #   "players": [...]
    # }
    # --------------------------------------------------------

    if isinstance(database, list):

        players = database
        wrapper = None

    elif isinstance(database, dict):

        if isinstance(
            database.get("players"),
            list,
        ):

            players = database[
                "players"
            ]

            wrapper = database

        else:

            print(
                "ERROR: JSON object does not "
                "contain a 'players' list."
            )

            sys.exit(1)

    else:

        print(
            "ERROR: Unsupported JSON structure."
        )

        sys.exit(1)

    print(
        f"Players available: {len(players):,}"
    )

    # --------------------------------------------------------
    # Limit for testing.
    # --------------------------------------------------------

    if args.limit > 0:

        target_total = min(
            args.limit,
            len(players),
        )

    else:

        target_total = len(players)

    print(
        f"Players to process: "
        f"{target_total:,}"
    )

    # --------------------------------------------------------
    # Existing checkpoint.
    # --------------------------------------------------------

    results = []
    diagnostics = []
    start_index = 0

    checkpoint = None

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
            [],
        )

        diagnostics = checkpoint.get(
            "diagnostics",
            [],
        )

        start_index = safe_int(
            checkpoint.get(
                "nextIndex",
                len(results),
            ),
            len(results),
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

        print()
        print(
            "Starting fresh."
        )

    # --------------------------------------------------------
    # Protect against checkpoint beyond limit.
    # --------------------------------------------------------

    if start_index >= target_total:

        print()
        print(
            "Nothing left to process "
            "for the requested limit."
        )

        print_summary(
            results,
            diagnostics,
        )

        return

    # --------------------------------------------------------
    # Process players.
    # --------------------------------------------------------

    for index in range(
        start_index,
        target_total,
    ):

        player = players[index]

        name = (
            player.get("playerName")
            or player.get("name")
            or player.get("fullName")
            or "UNKNOWN"
        )

        print(
            f"[{index + 1:,}/{target_total:,}] "
            f"{name}",
            end=" ",
            flush=True,
        )

        try:

            enriched, diagnostic = (
                enrich_player(
                    player,
                    index,
                    target_total,
                )
            )

            results.append(
                enriched
            )

            diagnostics.append(
                diagnostic
            )

            if diagnostic[
                "status"
            ] == "matched":

                print(
                    f"→ MATCH "
                    f"{diagnostic['wikidataId']} "
                    f"({diagnostic['reason']})"
                )

            else:

                print(
                    f"→ UNMATCHED "
                    f"({diagnostic['reason']})"
                )

        except KeyboardInterrupt:

            print()
            print(
                "Interrupted by user."
            )

            print(
                "Saving checkpoint..."
            )

            save_checkpoint(
                checkpoint_path,
                results,
                diagnostics,
                index,
            )

            print(
                "Checkpoint saved."
            )

            raise

        except Exception as exc:

            print(
                f"→ ERROR: {exc}"
            )

            # Preserve the original record.
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
                    "enrichmentCheckedAt":
                        datetime.utcnow().isoformat()
                        + "Z",
                }
            )

            results.append(
                error_result
            )

            diagnostics.append(
                {
                    "index": index,
                    "playerName": str(name),
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
        # Checkpoint.
        # ----------------------------------------------------

        processed = index + 1

        if (
            processed % CHECKPOINT_EVERY == 0
            or processed == target_total
        ):

            print()
            print(
                f"Saving checkpoint at "
                f"{processed:,}..."
            )

            save_checkpoint(
                checkpoint_path,
                results,
                diagnostics,
                processed,
            )

            write_diagnostics(
                diagnostics_path,
                diagnostics,
            )

            print(
                "Checkpoint saved."
            )

            print()

    # ========================================================
    # FINAL OUTPUT
    # ========================================================

    if wrapper is not None:

        final_database = dict(
            wrapper
        )

        final_database[
            "players"
        ] = results

    else:

        final_database = results

    print()
    print(
        "Writing final enriched database..."
    )

    save_json(
        output_path,
        final_database,
    )

    write_diagnostics(
        diagnostics_path,
        diagnostics,
    )

    print(
        "Final database written:"
    )

    print(
        output_path.resolve()
    )

    print()
    print_summary(
        results,
        diagnostics,
    )

    print()
    print(
        "Diagnostics written:"
    )

    print(
        diagnostics_path.resolve()
    )

    print()
    print(
        "DONE."
    )


# ============================================================
# ENTRY POINT
# ============================================================

if __name__ == "__main__":
    main()
