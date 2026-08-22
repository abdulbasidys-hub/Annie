"""Helius adapter — primary Solana blockchain infrastructure (§9.1).

This is the system's highest-trust source. Where Helius and an indexer
disagree about a chain fact (who deployed a mint, whether a mint exists, what
the metadata says), Helius wins and the disagreement is recorded (§21).

Two Helius surfaces are used:

* **RPC** (``HELIUS_RPC_URL``) — standard Solana JSON-RPC plus the DAS
  extensions ``getAsset`` / ``getAssetBatch``.
* **Enhanced API** (``api.helius.xyz``) — parsed transaction history, used to
  find a mint's deployer.

Note that the rest of the application never imports this module. It receives a
``BlockchainProvider`` from the registry, so replacing Helius means writing one
new adapter and changing one line of wiring (§9.1, §70).
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Any
from urllib.parse import parse_qsl, urlsplit

import structlog

from app.providers.http import HttpProvider
from app.providers.interfaces import ProviderError
from app.providers.types import HolderStats, Provenance, TokenLaunch, TokenMetadata

log = structlog.get_logger(__name__)

ENHANCED_API_BASE = "https://api.helius.xyz"

#: Launchpad program IDs this adapter scans for new mints, keyed to the slug
#: recorded on the resulting :class:`TokenLaunch`. Verified against public
#: sources (Solscan) on 2026-08-21 — see Build.md §9.2.
#:
#: This is a **starter list, not ecosystem coverage**. A program not listed
#: here is invisible to :meth:`HeliusAdapter.discover_launches` no matter how
#: many tokens it launches. Build.md §5 asks the system to notice new
#: launchpads automatically; doing that from raw RPC alone would mean
#: watching all SPL Token Program mint-creation activity network-wide, which
#: is an indexer's job (Bitquery, in the original design) rather than
#: something a polling adapter can do cheaply. Add a program ID here to
#: extend coverage; see README's "picking up the unfinished work" for the
#: larger fix.
KNOWN_LAUNCHPAD_PROGRAMS: dict[str, str] = {
    "6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P": "pump-fun",
}


class HeliusAdapter(HttpProvider):
    """Implements ``BlockchainProvider`` and ``TokenMetadataProvider``."""

    name = "helius"
    cost_per_request_usd = 0.0  # credit-metered plan; see ProviderEvent.request_units

    def __init__(self, api_key: str, rpc_url: str) -> None:
        self._api_key = (api_key or "").strip()
        self._rpc_url = (rpc_url or "").strip()

        # The RPC URL Helius issues carries the key as a query string
        # (?api-key=...). That query string is split off here and sent as an
        # explicit request param instead of left in base_url: httpx's
        # base_url + relative-path joining mishandles a query string already
        # present in base_url when the request path is "" or "/" — it
        # silently appends a stray "/" *inside* the query value, corrupting
        # the key into "...<key>/" and turning every single call into a 401.
        # Confirmed by comparing an identical request made directly (worked)
        # against one made through this adapter (401'd) — see git history.
        parsed = urlsplit(self._rpc_url) if self._rpc_url else None
        origin = f"{parsed.scheme}://{parsed.netloc}{parsed.path}" if parsed and parsed.netloc else ""
        self._rpc_params: dict[str, str] = dict(parse_qsl(parsed.query)) if parsed and parsed.query else {}

        # Base URL is the RPC endpoint (query-string-free); the enhanced API
        # is called with an absolute URL through the same instrumented
        # client so both surfaces appear under one provider on the health page.
        super().__init__(
            base_url=origin or "https://mainnet.helius-rpc.com",
            headers={"Content-Type": "application/json"},
            rate_per_second=10.0,
            burst=20,
            timeout_seconds=25.0,
        )
        self._rpc_id = 0
        self._id_lock = asyncio.Lock()

    def is_configured(self) -> bool:
        # Both are required. A key without an RPC URL is a half-configured
        # adapter, which the capability check reports as DEGRADED rather than
        # letting it fail mysteriously at the first call.
        return bool(self._api_key and self._rpc_url)

    async def _healthcheck(self) -> bool:
        result = await self._rpc("getHealth", [], operation="healthcheck")
        return result == "ok"

    # -- RPC plumbing ---------------------------------------------------------

    async def _next_id(self) -> int:
        async with self._id_lock:
            self._rpc_id += 1
            return self._rpc_id

    async def _rpc(self, method: str, params: Any, *, operation: str) -> Any:
        body = {
            "jsonrpc": "2.0",
            "id": await self._next_id(),
            "method": method,
            "params": params,
        }
        data = await self.request(
            "POST", "", operation=operation, json=body, params=self._rpc_params or None
        )
        if data is None:
            return None
        if "error" in data:
            err = data["error"] or {}
            code = err.get("code")
            raise ProviderError(
                self.name,
                operation,
                f"RPC error {code}: {err.get('message')}",
                # -32005 is node-behind / rate related; other codes are answers.
                retryable=code in (-32005, -32603),
            )
        return data.get("result")

    # -- BlockchainProvider ---------------------------------------------------

    async def verify_token_exists(self, mint: str) -> bool:
        result = await self._rpc(
            "getAccountInfo",
            [mint, {"encoding": "jsonParsed"}],
            operation="verify_token_exists",
        )
        return bool(result and result.get("value"))

    async def get_token_metadata(self, mint: str) -> TokenMetadata | None:
        result = await self._rpc(
            "getAsset", {"id": mint}, operation="get_token_metadata"
        )
        if not result:
            return None
        return self._parse_asset(mint, result)

    async def get_token_metadata_batch(
        self, mints: list[str]
    ) -> dict[str, TokenMetadata]:
        out: dict[str, TokenMetadata] = {}
        # DAS caps getAssetBatch at 1000 ids; 100 keeps individual responses
        # small enough that one slow mint does not stall a whole enrichment run.
        for chunk in _chunks(mints, 100):
            result = await self._rpc(
                "getAssetBatch",
                {"ids": chunk},
                operation="get_token_metadata_batch",
            )
            for asset in result or []:
                if not asset:
                    continue
                mint = asset.get("id")
                if not mint:
                    continue
                parsed = self._parse_asset(mint, asset)
                if parsed is not None:
                    out[mint] = parsed
        return out

    async def get_creator_wallet(self, mint: str) -> str | None:
        """Resolve the deployer by walking to the mint's oldest transaction.

        Indexers usually surface the fee payer, which for launchpad deployments
        is often the launchpad's own relayer rather than the person who created
        the token. §17's creator statistics would be meaningless if every
        Pump.fun token resolved to the same wallet, so this reads the earliest
        signature touching the mint and takes its first signer.

        Returns ``None`` — never a guess — when history is unavailable.
        """
        signatures = await self._rpc(
            "getSignaturesForAddress",
            [mint, {"limit": 1000}],
            operation="get_creator_wallet",
        )
        if not signatures:
            return None

        # getSignaturesForAddress returns newest-first; the deployment is the
        # oldest entry available. If the account has more than 1000 signatures
        # we may not have reached genesis, so we say so rather than guessing.
        oldest = signatures[-1]
        if len(signatures) >= 1000:
            log.info(
                "creator_lookup_truncated",
                mint=mint,
                note="more than 1000 signatures; deployer may predate this window",
            )
            return None

        signature = oldest.get("signature")
        if not signature:
            return None

        tx = await self._rpc(
            "getTransaction",
            [
                signature,
                {"encoding": "jsonParsed", "maxSupportedTransactionVersion": 0},
            ],
            operation="get_creator_wallet_tx",
        )
        if not tx:
            return None

        try:
            accounts = tx["transaction"]["message"]["accountKeys"]
        except (KeyError, TypeError):
            return None

        for account in accounts:
            if isinstance(account, dict):
                if account.get("signer"):
                    return account.get("pubkey")
            elif isinstance(account, str):
                return account
        return None

    # -- LaunchDataProvider (partial) -----------------------------------------
    # Only ``discover_launches`` is implemented. ``discover_launchpads`` and
    # ``get_migrations`` are not — nothing in this deployment's pipeline calls
    # them yet (see app/pipeline/discovery.py).

    async def discover_launches(
        self,
        since: datetime,
        until: datetime | None = None,
        limit: int = 1000,
        launchpad_slug: str | None = None,
    ) -> list[TokenLaunch]:
        """Scan known launchpad programs for new mints (§5, §20).

        For each tracked program (:data:`KNOWN_LAUNCHPAD_PROGRAMS`), walks its
        most recent signatures — newest first, capped at ``limit`` per program
        per call — and decodes any SPL Token ``initializeMint`` /
        ``initializeMint2`` instruction in the parsed transaction. That is a
        robust way to find the mint address without depending on a
        launchpad's own bespoke instruction layout: every SPL token, on any
        launchpad, is created through the standard Token Program.

        Two honest limitations, both by design rather than oversight:

        * **A program not in the known list is invisible.** This is a
          starter-list scanner, not the ecosystem-wide sweep Build.md §5
          describes — see that constant's docstring.
        * **One page per call.** ``getSignaturesForAddress`` returns at most
          1000 signatures; a launchpad producing more than that between two
          discovery runs will have some launches skipped. Fine for a system
          polling every few minutes; would need cursor-based pagination
          (tracking the last-seen signature per program) for a system that
          runs discovery hourly or less often.
        """
        programs = KNOWN_LAUNCHPAD_PROGRAMS
        if launchpad_slug:
            programs = {pid: slug for pid, slug in programs.items() if slug == launchpad_slug}

        since_ts = since.timestamp()
        until_ts = until.timestamp() if until else None

        launches: list[TokenLaunch] = []
        seen_mints: set[str] = set()

        for program_id, slug in programs.items():
            try:
                signatures = await self._rpc(
                    "getSignaturesForAddress",
                    [program_id, {"limit": min(limit, 1000)}],
                    operation="discover_launches_signatures",
                )
            except ProviderError as exc:
                log.warning(
                    "discover_launches_signatures_failed", program=program_id, error=str(exc)
                )
                continue
            if not signatures:
                continue

            for entry in signatures:
                block_time = entry.get("blockTime")
                if block_time is None:
                    continue
                if block_time < since_ts:
                    break  # newest-first: anything older means this page is done
                if until_ts is not None and block_time > until_ts:
                    continue
                if entry.get("err") is not None:
                    continue  # a failed transaction never created a real mint

                signature = entry.get("signature")
                if not signature:
                    continue

                try:
                    mints, fee_payer = await self._mints_and_fee_payer(signature)
                except ProviderError as exc:
                    log.info(
                        "discover_launches_tx_failed", signature=signature, error=str(exc)
                    )
                    continue
                if not mints:
                    continue

                launched_at = datetime.fromtimestamp(block_time, tz=timezone.utc)
                for mint in mints:
                    if mint in seen_mints:
                        continue
                    seen_mints.add(mint)
                    launches.append(
                        TokenLaunch(
                            mint=mint,
                            provenance=Provenance(
                                provider=self.name,
                                operation="discover_launches",
                                observed_at=datetime.now(timezone.utc),
                                raw_reference=signature,
                            ),
                            creator_wallet=fee_payer,
                            launchpad_slug=slug,
                            launched_at=launched_at,
                            signature=signature,
                        )
                    )
        return launches

    async def _mints_and_fee_payer(self, signature: str) -> tuple[list[str], str | None]:
        """One ``getTransaction`` call -> every mint it created, plus its fee payer.

        Scans both top-level and inner instructions: a launchpad program's own
        instruction routinely issues the Token Program's ``initializeMint``
        as a CPI, which only shows up under ``meta.innerInstructions``.
        """
        tx = await self._rpc(
            "getTransaction",
            [signature, {"encoding": "jsonParsed", "maxSupportedTransactionVersion": 0}],
            operation="discover_launches_tx",
        )
        if not tx:
            return [], None

        instructions: list[dict[str, Any]] = []
        try:
            instructions.extend(tx["transaction"]["message"]["instructions"] or [])
        except (KeyError, TypeError):
            pass
        meta = tx.get("meta") or {}
        for inner in meta.get("innerInstructions") or []:
            instructions.extend(inner.get("instructions") or [])

        mints: list[str] = []
        for ix in instructions:
            if not isinstance(ix, dict) or ix.get("program") != "spl-token":
                continue
            parsed = ix.get("parsed") or {}
            if parsed.get("type") not in ("initializeMint", "initializeMint2"):
                continue
            mint = (parsed.get("info") or {}).get("mint")
            if mint:
                mints.append(mint)

        fee_payer: str | None = None
        try:
            accounts = tx["transaction"]["message"]["accountKeys"]
            for account in accounts:
                if isinstance(account, dict) and account.get("signer"):
                    fee_payer = account.get("pubkey")
                    break
                if isinstance(account, str):
                    fee_payer = account
                    break
        except (KeyError, TypeError):
            pass

        return mints, fee_payer

    async def get_holder_stats(self, mint: str) -> HolderStats | None:
        """Concentration among the largest accounts.

        ``getTokenLargestAccounts`` returns at most 20 accounts, so this yields
        top-holder *share* but not a total holder count — which is why
        ``holder_count`` stays ``None`` here and is sourced from an indexer
        instead. Reporting 20 as the holder count would be badly wrong in a way
        that looks plausible.
        """
        largest = await self._rpc(
            "getTokenLargestAccounts", [mint], operation="get_holder_stats"
        )
        supply = await self._rpc("getTokenSupply", [mint], operation="get_token_supply")

        if not largest or not supply:
            return None

        total = _dec((supply.get("value") or {}).get("amount"))
        if total in (None, Decimal(0)):
            return None

        values = largest.get("value") or []
        top_10 = sum(
            (_dec(v.get("amount")) or Decimal(0)) for v in values[:10]
        )

        return HolderStats(
            mint=mint,
            provenance=Provenance(
                provider=self.name,
                operation="get_holder_stats",
                observed_at=datetime.now(timezone.utc),
            ),
            holder_count=None,
            top_10_share=(top_10 / total) if total else None,
        )

    # -- Enhanced API ---------------------------------------------------------

    async def get_parsed_transactions(
        self, address: str, limit: int = 100
    ) -> list[dict[str, Any]]:
        """Human-readable transaction history for a wallet or mint.

        Used by creator analysis to observe launch cadence (§17) without
        building the full wallet graph §17 explicitly defers.
        """
        url = f"{ENHANCED_API_BASE}/v0/addresses/{address}/transactions"
        data = await self.request(
            "GET",
            url,
            operation="get_parsed_transactions",
            params={"api-key": self._api_key, "limit": limit},
            allow_404=True,
        )
        return data or []

    # -- parsing --------------------------------------------------------------

    def _parse_asset(self, mint: str, asset: dict[str, Any]) -> TokenMetadata | None:
        content = asset.get("content") or {}
        metadata = content.get("metadata") or {}
        links = content.get("links") or {}
        token_info = asset.get("token_info") or {}

        image_url = links.get("image")
        if not image_url:
            for file in content.get("files") or []:
                if isinstance(file, dict) and file.get("uri"):
                    image_url = file["uri"]
                    break

        # DAS `creators` is the Metaplex creator array — royalty recipients,
        # which are frequently not the deployer. Recorded, but the deployer
        # comes from get_creator_wallet.
        creators = asset.get("creators") or []
        metaplex_creator = None
        for creator in creators:
            if isinstance(creator, dict) and creator.get("verified"):
                metaplex_creator = creator.get("address")
                break

        supply = _dec(token_info.get("supply"))
        decimals = token_info.get("decimals")
        if supply is not None and isinstance(decimals, int) and decimals > 0:
            supply = supply / (Decimal(10) ** decimals)

        return TokenMetadata(
            mint=mint,
            provenance=Provenance(
                provider=self.name,
                operation="get_asset",
                observed_at=datetime.now(timezone.utc),
                raw_reference=mint,
            ),
            name=metadata.get("name"),
            symbol=metadata.get("symbol") or token_info.get("symbol"),
            description=metadata.get("description"),
            image_url=image_url,
            decimals=decimals if isinstance(decimals, int) else None,
            total_supply=supply,
            creator_wallet=metaplex_creator,
            website=links.get("external_url"),
            other_links={k: v for k, v in links.items() if isinstance(v, str)},
        )


def _dec(value: Any) -> Decimal | None:
    if value is None or value == "":
        return None
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        return None


def _chunks(items: list[str], size: int) -> list[list[str]]:
    return [items[i : i + size] for i in range(0, len(items), size)]
