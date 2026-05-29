"""Order builder — constructs orders from position sizing results.

Creates order specifications that can be routed to the CLOB.
Supports:
  - Limit and market orders with slippage protection
  - TWAP (time-weighted average price) splitting for large orders
  - Iceberg orders (hidden size)
  - Adaptive pricing based on orderbook depth
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from typing import Any

from src.config import ExecutionConfig
from src.policy.position_sizer import PositionSize
from src.observability.logger import get_logger

log = get_logger(__name__)


@dataclass
class OrderSpec:
    """Specification for an order to be placed."""
    order_id: str
    market_id: str
    token_id: str
    side: str           # "BUY" | "SELL"
    order_type: str     # "limit" | "market"
    price: float        # limit price (or 0 for market)
    size: float         # token quantity
    stake_usd: float    # USD value
    ttl_secs: int       # time-to-live for limit orders
    dry_run: bool       # if True, order is simulated only
    metadata: dict[str, Any] = field(default_factory=dict)

    # Smart execution fields
    execution_strategy: str = "simple"  # "simple" | "twap" | "iceberg" | "adaptive"
    parent_order_id: str = ""  # for child orders in TWAP/iceberg
    child_index: int = 0
    total_children: int = 1

    def to_dict(self) -> dict[str, Any]:
        return {
            "order_id": self.order_id,
            "market_id": self.market_id,
            "token_id": self.token_id,
            "side": self.side,
            "order_type": self.order_type,
            "price": self.price,
            "size": self.size,
            "stake_usd": self.stake_usd,
            "ttl_secs": self.ttl_secs,
            "dry_run": self.dry_run,
            "execution_strategy": self.execution_strategy,
        }


def build_order(
    market_id: str,
    token_id: str,
    position: PositionSize,
    implied_price: float,
    config: ExecutionConfig,
    orderbook_depth: float = 0.0,
    execution_strategy: str = "auto",
) -> list[OrderSpec]:
    """Build order specification(s) from a position sizing result.

    For large orders, returns multiple child orders (TWAP/iceberg).
    For small orders, returns a single order.

    Args:
        execution_strategy: "auto" selects based on order size vs depth.
            "simple" = single order, "twap" = time-split, "iceberg" = hidden
    """
    side = "BUY"  # We always buy tokens (YES or NO)

    # Auto-select strategy based on order size vs market depth
    if execution_strategy == "auto":
        if orderbook_depth > 0 and position.stake_usd > orderbook_depth * 0.3:
            # Order is >30% of visible depth — use TWAP
            execution_strategy = "twap"
        elif position.stake_usd > 500:
            # Medium-large order — use iceberg
            execution_strategy = "iceberg"
        else:
            execution_strategy = "simple"

    if execution_strategy == "twap":
        return _build_twap_orders(
            market_id, token_id, position, implied_price, config
        )
    elif execution_strategy == "iceberg":
        return _build_iceberg_orders(
            market_id, token_id, position, implied_price, config
        )
    else:
        return [_build_simple_order(
            market_id, token_id, position, implied_price, config
        )]


def _build_simple_order(
    market_id: str,
    token_id: str,
    position: PositionSize,
    implied_price: float,
    config: ExecutionConfig,
) -> OrderSpec:
    """Build a single order."""
    if config.default_order_type == "limit":
        price = round(implied_price * (1 + config.slippage_tolerance), 4)
    else:
        price = 0.0

    order = OrderSpec(
        order_id=str(uuid.uuid4()),
        market_id=market_id,
        token_id=token_id,
        side="BUY",
        order_type=config.default_order_type,
        price=price,
        size=position.token_quantity,
        stake_usd=position.stake_usd,
        ttl_secs=config.limit_order_ttl_secs,
        dry_run=config.dry_run,
        execution_strategy="simple",
        metadata={
            "direction": position.direction,
            "kelly_fraction": position.kelly_fraction_used,
            "capped_by": position.capped_by,
        },
    )

    log.info(
        "order_builder.built",
        order_id=order.order_id[:8],
        market_id=market_id,
        side="BUY",
        type=order.order_type,
        price=order.price,
        size=order.size,
        dry_run=order.dry_run,
    )
    return order


def _build_twap_orders(
    market_id: str,
    token_id: str,
    position: PositionSize,
    implied_price: float,
    config: ExecutionConfig,
    num_slices: int = 5,
) -> list[OrderSpec]:
    """Split order into time-weighted slices.

    Each slice is slightly smaller and has progressively better pricing
    to encourage patient execution.
    """
    parent_id = str(uuid.uuid4())
    orders: list[OrderSpec] = []
    slice_size = position.token_quantity / num_slices
    slice_stake = position.stake_usd / num_slices

    for i in range(num_slices):
        # Each slice gets slightly tighter pricing
        # First slice: full slippage tolerance
        # Last slice: half slippage tolerance (more aggressive)
        slippage = config.slippage_tolerance * (1 - i * 0.1)
        price = round(implied_price * (1 + slippage), 4)

        order = OrderSpec(
            order_id=str(uuid.uuid4()),
            market_id=market_id,
            token_id=token_id,
            side="BUY",
            order_type="limit",
            price=price,
            size=round(slice_size, 2),
            stake_usd=round(slice_stake, 2),
            ttl_secs=config.limit_order_ttl_secs,
            dry_run=config.dry_run,
            execution_strategy="twap",
            parent_order_id=parent_id,
            child_index=i,
            total_children=num_slices,
            metadata={
                "direction": position.direction,
                "kelly_fraction": position.kelly_fraction_used,
                "twap_slice": f"{i+1}/{num_slices}",
            },
        )
        orders.append(order)

    log.info(
        "order_builder.twap",
        parent_id=parent_id[:8],
        market_id=market_id,
        total_size=position.token_quantity,
        slices=num_slices,
        dry_run=config.dry_run,
    )
    return orders


def _build_iceberg_orders(
    market_id: str,
    token_id: str,
    position: PositionSize,
    implied_price: float,
    config: ExecutionConfig,
    visible_pct: float = 0.20,
) -> list[OrderSpec]:
    """Build iceberg order — only show a portion of total size.

    For markets without native iceberg support, we simulate by
    placing a small visible order and queueing the rest.
    """
    parent_id = str(uuid.uuid4())
    visible_size = position.token_quantity * visible_pct
    hidden_size = position.token_quantity * (1 - visible_pct)

    price = round(implied_price * (1 + config.slippage_tolerance), 4)

    # Visible portion
    visible_order = OrderSpec(
        order_id=str(uuid.uuid4()),
        market_id=market_id,
        token_id=token_id,
        side="BUY",
        order_type="limit",
        price=price,
        size=round(visible_size, 2),
        stake_usd=round(position.stake_usd * visible_pct, 2),
        ttl_secs=config.limit_order_ttl_secs,
        dry_run=config.dry_run,
        execution_strategy="iceberg",
        parent_order_id=parent_id,
        child_index=0,
        total_children=2,
        metadata={
            "direction": position.direction,
            "iceberg_part": "visible",
            "total_size": position.token_quantity,
        },
    )

    # Hidden portion (to be placed after visible fills)
    hidden_order = OrderSpec(
        order_id=str(uuid.uuid4()),
        market_id=market_id,
        token_id=token_id,
        side="BUY",
        order_type="limit",
        price=price,
        size=round(hidden_size, 2),
        stake_usd=round(position.stake_usd * (1 - visible_pct), 2),
        ttl_secs=config.limit_order_ttl_secs,
        dry_run=config.dry_run,
        execution_strategy="iceberg",
        parent_order_id=parent_id,
        child_index=1,
        total_children=2,
        metadata={
            "direction": position.direction,
            "iceberg_part": "hidden",
            "total_size": position.token_quantity,
        },
    )

    log.info(
        "order_builder.iceberg",
        parent_id=parent_id[:8],
        market_id=market_id,
        visible_size=round(visible_size, 2),
        hidden_size=round(hidden_size, 2),
        dry_run=config.dry_run,
    )
    return [visible_order, hidden_order]
