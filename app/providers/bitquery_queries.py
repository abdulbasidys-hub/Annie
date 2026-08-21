"""GraphQL documents for the Bitquery adapter.

Separated from the adapter deliberately. Bitquery's Solana schema lives on
their EAP endpoint and changes more often than a stable public API would, so
isolating the query text means a schema change is a one-file edit that never
touches response mapping, retry logic or the DTO boundary.

**These queries need verification against a live Bitquery account before first
production run.** They are written against the documented Solana EAP schema,
but field availability varies by plan tier, and an incorrect field name fails
loudly at the adapter rather than silently returning empty results — which is
the behaviour we want, because an empty launch feed that looks successful would
quietly halt all research.

Every query takes an explicit time window. Bitquery bills by processed data, so
unbounded queries are both slow and expensive, and §48 makes cost control a
design constraint rather than an optimisation.
"""

from __future__ import annotations

# -----------------------------------------------------------------------------
# Known launch program addresses.
#
# This is a *seed list*, not a definition of the universe. §5 forbids assuming
# successful tokens originate from Pump.fun, so discovery also runs unfiltered
# and any launch program not in this map produces a LaunchpadSignal that
# creates a new launchpad row. The map exists to give known programs readable
# slugs, not to restrict what gets collected.
# -----------------------------------------------------------------------------

KNOWN_LAUNCH_PROGRAMS: dict[str, str] = {
    "6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P": "pumpfun",
    "pAMMBay6oceH9fJKBRHGP5D4bD4sWpmSwMn52FMfXEA": "pumpswap",
    "LBUZKhRxPF3XUpBCjp4YzTKgLccjZhTSDM9YuVaPwxo": "meteora-dlmm",
    "Eo7WjKq67rjJQSZxS6z3YkapzY3eMj6Xy8X5EQVn5UaB": "meteora-pools",
    "675kPX9MHTjS2zt1qfr1NYHuzeLXfQM9H24wFSUt1Mp8": "raydium-amm-v4",
    "CAMMCzo5YL8w4VFF8KVHrK22GGUsp5VTaW7grrKgrWqK": "raydium-clmm",
    "whirLbMiicVdio4qvUfM5KAg6Ct8VwpYzGff3uctyCc": "orca-whirlpool",
}

KNOWN_DEX_PROGRAMS: dict[str, str] = {
    "675kPX9MHTjS2zt1qfr1NYHuzeLXfQM9H24wFSUt1Mp8": "raydium",
    "CAMMCzo5YL8w4VFF8KVHrK22GGUsp5VTaW7grrKgrWqK": "raydium-clmm",
    "pAMMBay6oceH9fJKBRHGP5D4bD4sWpmSwMn52FMfXEA": "pumpswap",
    "whirLbMiicVdio4qvUfM5KAg6Ct8VwpYzGff3uctyCc": "orca",
    "LBUZKhRxPF3XUpBCjp4YzTKgLccjZhTSDM9YuVaPwxo": "meteora",
}


# -----------------------------------------------------------------------------
# Discovery
# -----------------------------------------------------------------------------

#: New token mints in a window, with the program that created them.
#:
#: Sourced from TokenSupplyUpdates rather than a launchpad-specific instruction
#: feed, because every token creation produces a supply update regardless of
#: which platform issued it. That is what makes discovery launchpad-agnostic.
LAUNCHES_QUERY = """
query Launches($since: DateTime!, $until: DateTime!, $limit: Int!) {
  Solana(dataset: combined) {
    TokenSupplyUpdates(
      where: {
        Block: { Time: { since: $since, till: $until } }
        TokenSupplyUpdate: { Marketcap: { PostBalance: { gt: "0" } } }
      }
      orderBy: { ascending: Block_Time }
      limit: { count: $limit }
    ) {
      Block {
        Time
        Slot
      }
      Transaction {
        Signature
        Signer
      }
      Instruction {
        Program {
          Address
          Name
          Method
        }
      }
      TokenSupplyUpdate {
        Currency {
          MintAddress
          Name
          Symbol
          Decimals
          Uri
        }
        PostBalance
      }
    }
  }
}
"""

#: Launch programs active in a window, with counts.
#:
#: This is the query that answers "did a new launchpad appear?" (§5, §18).
#: It groups by program address without filtering to known ones, so a platform
#: nobody has heard of shows up the first week it has volume.
LAUNCHPAD_ACTIVITY_QUERY = """
query LaunchpadActivity($since: DateTime!, $until: DateTime!) {
  Solana(dataset: combined) {
    Instructions(
      where: {
        Block: { Time: { since: $since, till: $until } }
        Instruction: { Program: { Method: { in: ["create", "initialize", "initialize2"] } } }
        Transaction: { Result: { Success: true } }
      }
      orderBy: { descendingByField: "count" }
      limit: { count: 100 }
    ) {
      count: count
      Instruction {
        Program {
          Address
          Name
        }
      }
      Block {
        first_seen: Time(minimum: Block_Time)
        last_seen: Time(maximum: Block_Time)
      }
    }
  }
}
"""

#: Tokens graduating from a bonding curve to a DEX pool (§19).
MIGRATIONS_QUERY = """
query Migrations($since: DateTime!, $until: DateTime!, $limit: Int!) {
  Solana(dataset: combined) {
    DEXPools(
      where: {
        Block: { Time: { since: $since, till: $until } }
        Pool: { Base: { PostAmount: { gt: "0" } } }
        Transaction: { Result: { Success: true } }
      }
      orderBy: { ascending: Block_Time }
      limit: { count: $limit }
    ) {
      Block {
        Time
      }
      Transaction {
        Signature
      }
      Pool {
        Market {
          MarketAddress
          BaseCurrency {
            MintAddress
            Symbol
          }
          QuoteCurrency {
            MintAddress
            Symbol
          }
        }
        Dex {
          ProgramAddress
          ProtocolName
          ProtocolFamily
        }
        Base {
          PostAmount
        }
        Quote {
          PostAmount
          PostAmountInUSD
        }
      }
    }
  }
}
"""


# -----------------------------------------------------------------------------
# Market data
# -----------------------------------------------------------------------------

#: Most recent trade for each of a set of mints — the current quote.
LATEST_QUOTE_QUERY = """
query LatestQuote($mints: [String!]!, $since: DateTime!) {
  Solana(dataset: combined) {
    DEXTradeByTokens(
      where: {
        Trade: { Currency: { MintAddress: { in: $mints } } }
        Block: { Time: { since: $since } }
      }
      orderBy: { descending: Block_Time }
      limit: { count: 200 }
    ) {
      Block {
        Time
      }
      Trade {
        Currency {
          MintAddress
          Symbol
          Decimals
        }
        PriceInUSD
        Market {
          MarketAddress
        }
        Dex {
          ProtocolName
          ProgramAddress
        }
      }
    }
  }
}
"""

#: OHLCV bars for one mint.
OHLCV_QUERY = """
query Ohlcv($mint: String!, $since: DateTime!, $until: DateTime!, $interval: Int!) {
  Solana(dataset: combined) {
    DEXTradeByTokens(
      where: {
        Trade: { Currency: { MintAddress: { is: $mint } } }
        Block: { Time: { since: $since, till: $until } }
      }
      orderBy: { ascendingByField: "Block_Timefield" }
      limit: { count: 5000 }
    ) {
      Block {
        Timefield: Time(interval: { in: minutes, count: $interval })
      }
      volume: sum(of: Trade_Side_AmountInUSD)
      Trade {
        open: PriceInUSD(minimum: Block_Time)
        high: PriceInUSD(maximum: Trade_PriceInUSD)
        low: PriceInUSD(minimum: Trade_PriceInUSD)
        close: PriceInUSD(maximum: Block_Time)
      }
      count
    }
  }
}
"""

#: Highest market cap reached in a window, and when.
#:
#: Answered as an aggregate rather than by scanning bars client-side. §7 wants
#: the peak milestone without storing a time series, and letting the provider
#: compute the maximum is both cheaper and avoids transferring data we would
#: immediately discard.
PEAK_MARKETCAP_QUERY = """
query PeakMarketcap($mint: String!, $since: DateTime!, $until: DateTime!) {
  Solana(dataset: combined) {
    DEXTradeByTokens(
      where: {
        Trade: { Currency: { MintAddress: { is: $mint } } }
        Block: { Time: { since: $since, till: $until } }
      }
      limit: { count: 1 }
      orderBy: { descendingByField: "peak_marketcap" }
    ) {
      peak_marketcap: quantile(
        of: Trade_Market_MarketCap
        level: 1
      )
      Block {
        Time(maximum: Trade_Market_MarketCap)
      }
      Trade {
        Currency {
          MintAddress
        }
      }
    }
  }
}
"""

#: Newly created pools, used to catch migrations to DEXes we do not yet track.
NEW_POOLS_QUERY = """
query NewPools($since: DateTime!, $limit: Int!) {
  Solana(dataset: combined) {
    DEXPools(
      where: {
        Block: { Time: { since: $since } }
        Transaction: { Result: { Success: true } }
      }
      orderBy: { descending: Block_Time }
      limit: { count: $limit }
    ) {
      Block {
        Time
      }
      Pool {
        Market {
          MarketAddress
          BaseCurrency {
            MintAddress
            Symbol
          }
        }
        Dex {
          ProgramAddress
          ProtocolName
        }
        Quote {
          PostAmountInUSD
        }
      }
    }
  }
}
"""

#: Cheapest possible query, used only to prove the credential works.
HEALTHCHECK_QUERY = """
query Healthcheck {
  Solana(dataset: combined) {
    Blocks(limit: { count: 1 }, orderBy: { descending: Block_Time }) {
      Block {
        Time
        Height
      }
    }
  }
}
"""
