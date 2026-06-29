"""Balance reset and anti-prestige redemption."""

from __future__ import annotations

from config import STARTING_BALANCE
from database import Database
from models import UserBalance

REDEMPTION_COST = 2 * STARTING_BALANCE

_EXPOSURE_MSG = (
    "You have funds tied up in active bets. "
    "Resolve or cancel them before using this command."
)


class EconomyService:
    """Reset bailouts and anti-prestige redemption."""

    def __init__(self, db: Database) -> None:
        self.db = db

    async def reset_balance(self, guild_id: int, user_id: int) -> UserBalance:
        """Bail out to starting cash; increments anti-prestige."""
        await self.db.ensure_user(guild_id, user_id)
        user = await self.db.get_user(guild_id, user_id)
        assert user is not None

        if user.balance >= STARTING_BALANCE:
            raise ValueError(
                f"You can only reset when your balance is below **{STARTING_BALANCE}** coins."
            )
        if await self.db.has_active_exposure(guild_id, user_id):
            raise ValueError(_EXPOSURE_MSG)

        return await self.db.reset_user_balance(guild_id, user_id)

    async def redeem_reset(self, guild_id: int, user_id: int) -> UserBalance:
        """Pay 2× starting balance to remove one reset from anti-prestige."""
        await self.db.ensure_user(guild_id, user_id)
        user = await self.db.get_user(guild_id, user_id)
        assert user is not None

        if user.reset_count <= 0:
            raise ValueError("You have no anti-prestige to redeem.")
        if user.balance < REDEMPTION_COST:
            raise ValueError(
                f"Redemption costs **{REDEMPTION_COST}** coins "
                f"(2× starting balance of **{STARTING_BALANCE}**)."
            )
        if await self.db.has_active_exposure(guild_id, user_id):
            raise ValueError(_EXPOSURE_MSG)

        return await self.db.redeem_reset(guild_id, user_id, REDEMPTION_COST)
