import json
import time
import re
from pathlib import Path
from urllib.parse import urlencode
from urllib.request import Request, urlopen
from urllib.error import HTTPError, URLError


# ============================================================
# FYUCHA PLAYER DATABASE - WIKIDATA ENRICHMENT
# TEST VERSION
# ============================================================

INPUT_FILE = Path("output/players.json")

OUTPUT_FILE = Path(
    "output/enriched-players-test.json"
)

MATCH_FILE = Path(
    "output/wikidata-matches-test.json"
)

# ------------------------------------------------------------
# IMPORTANT:
# We deliberately test only 100 players first.
# After checking the results, we can increase this.
# ------------------------------------------------------------

TEST_LIMIT = 100

WIKIDATA_API = (
    "https://www.wikidata.org/w/api.php"
)

USER_AGENT = (
    "FyuchaFootballBirthdays/1.0 "
    "(https://github.com/openfootball/players)"
)


# ============================================================
# HTTP REQUEST
# ============================================================

def request_json(url, params=None):

    if params:

        url = (
            url
            + "?"
            + urlencode(params)
        )

    request = Request(
        url,
        headers={
            "User-Agent": USER_AGENT,
            "Accept": "application/json"
        }
    )

    with urlopen(
        request,
        timeout=30
    ) as response:

        return json.loads(
            response.read().decode("utf-8")
        )


# ============================================================
# WIKIDATA SEARCH
# ============================================================

def search_wikidata(name):

    params = {

        "action": "wbsearchentities",

        "search": name,

        "language": "en",

        "format": "json",

        "limit": 10,

        "type": "item"
    }

    try:

        data = request_json(
            WIKIDATA_API,
            params
        )

        return data.get(
            "search",
            []
        )

    except Exception as error:

        print(
            f"Search error for {name}: {error}"
        )

        return []


# ============================================================
# GET WIKIDATA ENTITY
# ============================================================

def get_entity(qid):

    params = {

        "action": "wbgetentities",

        "ids": qid,

        "format": "json",

        "languages": "en",

        "props": "labels|descriptions|aliases|claims"
    }

    try:

        data = request_json(
            WIKIDATA_API,
            params
        )

        return data.get(
            "entities",
            {}
        ).get(qid)

    except Exception as error:

        print(
            f"Entity error for {qid}: {error}"
        )

        return None


# ============================================================
# EXTRACT CLAIM ID
# ============================================================

def get_claim_id(
    claims,
    property_id
):

    values = claims.get(
        property_id,
        []
    )

    if not values:

        return None

    mainsnak = values[0].get(
        "mainsnak",
        {}
    )

    datavalue = mainsnak.get(
        "datavalue"
    )

    if not datavalue:

        return None

    value = datavalue.get(
        "value"
    )

    if isinstance(
        value,
        dict
    ):

        return value.get(
            "id"
        )

    return None


# ============================================================
# EXTRACT TIME CLAIM
# ============================================================

def get_claim_date(
    claims,
    property_id
):

    values = claims.get(
        property_id,
        []
    )

    if not values:

        return None

    mainsnak = values[0].get(
        "mainsnak",
        {}
    )

    datavalue = mainsnak.get(
        "datavalue"
    )

    if not datavalue:

        return None

    value = datavalue.get(
        "value"
    )

    if not isinstance(
        value,
        dict
    ):

        return None

    time_value = value.get(
        "time"
    )

    if not time_value:

        return None

    # Example:
    # +1998-12-20T00:00:00Z

    match = re.search(
        r"([+-]\d{4,})-(\d{2})-(\d{2})",
        time_value
    )

    if not match:

        return None

    year = match.group(1)

    month = match.group(2)

    day = match.group(3)

    year = year.lstrip("+")

    return (
        f"{year}-{month}-{day}"
    )


# ============================================================
# EXTRACT IMAGE
# ============================================================

def get_claim_string(
    claims,
    property_id
):

    values = claims.get(
        property_id,
        []
    )

    if not values:

        return None

    mainsnak = values[0].get(
        "mainsnak",
        {}
    )

    datavalue = mainsnak.get(
        "datavalue"
    )

    if not datavalue:

        return None

    value = datavalue.get(
        "value"
    )

    if isinstance(
        value,
        str
    ):

        return value

    return None


# ============================================================
# LABEL FOR A WIKIDATA Q-ID
# ============================================================

def get_entity_label(
    qid,
    cache
):

    if not qid:

        return None

    if qid in cache:

        return cache[qid]

    entity = get_entity(
        qid
    )

    if not entity:

        cache[qid] = None

        return None

    labels = entity.get(
        "labels",
        {}
    )

    label = None

    if "en" in labels:

        label = labels["en"].get(
            "value"
        )

    cache[qid] = label

    return label


# ============================================================
# NORMALIZE NAME FOR COMPARISON
# ============================================================

def normalize_name(name):

    name = name.lower()

    name = name.replace(
        "’",
        "'"
    )

    name = re.sub(
        r"[^a-z0-9 ]",
        "",
        name
    )

    name = re.sub(
        r"\s+",
        " ",
        name
    )

    return name.strip()


# ============================================================
# FIND THE BEST WIKIDATA MATCH
# ============================================================

def find_match(
    player,
    label_cache
):

    name = player.get(
        "name",
        ""
    )

    date_of_birth = player.get(
        "dateOfBirth"
    )

    if not name or not date_of_birth:

        return None

    search_results = search_wikidata(
        name
    )

    if not search_results:

        return None

    normalized_player_name = (
        normalize_name(name)
    )

    # --------------------------------------------------------
    # Check up to 10 search candidates.
    # The most important test is matching DOB.
    # --------------------------------------------------------

    for result in search_results:

        qid = result.get(
            "id"
        )

        if not qid:

            continue

        entity = get_entity(
            qid
        )

        if not entity:

            continue

        claims = entity.get(
            "claims",
            {}
        )

        wikidata_dob = get_claim_date(
            claims,
            "P569"
        )

        # ----------------------------------------------------
        # DATE OF BIRTH MUST MATCH
        # ----------------------------------------------------

        if wikidata_dob != date_of_birth:

            continue

        label = (
            entity
            .get("labels", {})
            .get("en", {})
            .get("value")
        )

        # ----------------------------------------------------
        # Extract useful information
        # ----------------------------------------------------

        nationality_id = get_claim_id(
            claims,
            "P27"
        )

        birthplace_id = get_claim_id(
            claims,
            "P19"
        )

        gender_id = get_claim_id(
            claims,
            "P21"
        )

        image = get_claim_string(
            claims,
            "P18"
        )

        death_date = get_claim_date(
            claims,
            "P570"
        )

        nationality = get_entity_label(
            nationality_id,
            label_cache
        )

        birthplace = get_entity_label(
            birthplace_id,
            label_cache
        )

        gender = get_entity_label(
            gender_id,
            label_cache
        )

        # ----------------------------------------------------
        # Career status
        # ----------------------------------------------------

        if death_date:

            career_status = (
                "deceased"
            )

        else:

            career_status = (
                "unknown"
            )

        return {

            "wikidataId": qid,

            "wikidataUrl":
                f"https://www.wikidata.org/wiki/{qid}",

            "matchedName": label,

            "dateOfBirth":
                wikidata_dob,

            "nationality":
                nationality,

            "nationalityId":
                nationality_id,

            "birthPlace":
                birthplace,

            "birthPlaceId":
                birthplace_id,

            "gender":
                gender,

            "genderId":
                gender_id,

            "photo":
                image,

            "deathDate":
                death_date,

            "careerStatus":
                career_status
        }

    return None


# ============================================================
# ENRICH PLAYERS
# ============================================================

def enrich_players():

    if not INPUT_FILE.exists():

        raise FileNotFoundError(
            f"Could not find {INPUT_FILE}"
        )

    print(
        "Loading Fyucha player database..."
    )

    with INPUT_FILE.open(
        "r",
        encoding="utf-8"
    ) as file:

        players = json.load(file)

    print(
        f"Total players available: "
        f"{len(players):,}"
    )

    # --------------------------------------------------------
    # TEST SAMPLE
    # --------------------------------------------------------

    test_players = players[
        :TEST_LIMIT
    ]

    print(
        f"Testing enrichment on "
        f"{len(test_players)} players."
    )

    enriched_players = []

    match_records = []

    label_cache = {}

    matched = 0

    not_matched = 0

    errors = 0

    for number, player in enumerate(
        test_players,
        start=1
    ):

        name = player.get(
            "name",
            "Unknown"
        )

        print(
            f"[{number}/{len(test_players)}] "
            f"Checking {name}"
        )

        updated_player = dict(
            player
        )

        try:

            match = find_match(
                player,
                label_cache
            )

            if match:

                matched += 1

                updated_player[
                    "wikidata"
                ] = match

                # --------------------------------------------
                # Add useful values to top-level fields
                # --------------------------------------------

                if match.get(
                    "nationality"
                ):

                    updated_player[
                        "nationality"
                    ] = match[
                        "nationality"
                    ]

                if match.get(
                    "birthPlace"
                ):

                    updated_player[
                        "birthPlace"
                    ] = match[
                        "birthPlace"
                    ]

                if match.get(
                    "gender"
                ):

                    updated_player[
                        "gender"
                    ] = match[
                        "gender"
                    ]

                if match.get(
                    "photo"
                ):

                    updated_player[
                        "photo"
                    ] = match[
                        "photo"
                    ]

                if match.get(
                    "careerStatus"
                ) == "deceased":

                    updated_player[
                        "careerStatus"
                    ] = "deceased"

                match_records.append({
                    "playerId":
                        player.get("id"),

                    "playerName":
                        name,

                    "dateOfBirth":
                        player.get(
                            "dateOfBirth"
                        ),

                    "wikidataId":
                        match.get(
                            "wikidataId"
                        ),

                    "matchedName":
                        match.get(
                            "matchedName"
                        )
                })

                print(
                    f"  MATCHED: "
                    f"{match.get('wikidataId')}"
                )

            else:

                not_matched += 1

                print(
                    "  No verified match"
                )

        except (
            HTTPError,
            URLError,
            TimeoutError
        ) as error:

            errors += 1

            print(
                f"  Network error: {error}"
            )

        except Exception as error:

            errors += 1

            print(
                f"  Error: {error}"
            )

        # ----------------------------------------------------
        # Small delay to be respectful to Wikidata
        # ----------------------------------------------------

        time.sleep(
            0.5
        )

        enriched_players.append(
            updated_player
        )

    # ========================================================
    # SAVE ENRICHED TEST DATABASE
    # ========================================================

    with OUTPUT_FILE.open(
        "w",
        encoding="utf-8"
    ) as file:

        json.dump(
            enriched_players,
            file,
            ensure_ascii=False,
            indent=2
        )

    # ========================================================
    # SAVE MATCH REPORT
    # ========================================================

    report = {

        "testLimit":
            TEST_LIMIT,

        "playersTested":
            len(test_players),

        "matched":
            matched,

        "notMatched":
            not_matched,

        "errors":
            errors,

        "matchRate":
            round(
                (
                    matched
                    / len(test_players)
                    * 100
                ),
                2
            )
            if test_players
            else 0,

        "matches":
            match_records
    }

    with MATCH_FILE.open(
        "w",
        encoding="utf-8"
    ) as file:

        json.dump(
            report,
            file,
            ensure_ascii=False,
            indent=2
        )

    print()
    print("=" * 60)
    print("FYUCHA WIKIDATA TEST COMPLETE")
    print("=" * 60)

    print(
        f"Players tested: {len(test_players)}"
    )

    print(
        f"Verified matches: {matched}"
    )

    print(
        f"No match: {not_matched}"
    )

    print(
        f"Errors: {errors}"
    )

    print(
        f"Match rate: {report['matchRate']}%"
    )

    print()
    print(
        f"Created: {OUTPUT_FILE}"
    )

    print(
        f"Created: {MATCH_FILE}"
    )

    print("=" * 60)


# ============================================================
# START
# ============================================================

if __name__ == "__main__":

    enrich_players()
