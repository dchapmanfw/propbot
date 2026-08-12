"""Rich embed market analysis with plotext terminal charts."""

from __future__ import annotations

import re
from datetime import datetime

import discord
import plotext as plt

from config import NO_EMOJI, YES_EMOJI
from lmsr import format_price_cents, lmsr_price_no, lmsr_price_yes
from models import Bet, BetStatus, MarketPriceSnapshot, MarketSnapshotEvent

_PLOTEXT_MAX_POINTS = 30
_PLOTEXT_WIDTH = 44
_PLOTEXT_HEIGHT = 14
_PLOTEXT_COMPACT_WIDTH = 42
_PLOTEXT_COMPACT_HEIGHT = 12
_PLOTEXT_SMALL_WIDTH = 36
_PLOTEXT_SMALL_HEIGHT = 10
_BOARD_PLOTEXT_MAX_POINTS = 30
# Light traffic: tall narrow plots — maximize Y (price) resolution.
_BOARD_PLOTEXT_LIGHT_SIZES: tuple[tuple[int, int], ...] = (
    (24, 20),
    (25, 20),
    (24, 19),
    (25, 19),
    (26, 19),
)
# Heavy traffic: only used when the light preset cannot fit in the field.
_BOARD_PLOTEXT_HEAVY_SIZES: tuple[tuple[int, int], ...] = (
    (24, 18),
    (26, 18),
    (28, 17),
    (30, 16),
    (28, 15),
    (26, 14),
    (24, 12),
    (32, 10),
)
_BOARD_PLOTEXT_HEAVY_POINT_FALLBACKS = (25, 20, 15, 12)
_PLOTEXT_Y_FREQUENCY = 25
_DISCORD_FIELD_LIMIT = 1024
_DISCORD_DESCRIPTION_LIMIT = 4096
_BOARD_LIST_QUESTION_LEN = 36
_MAX_MARKETS_PER_BOARD_PAGE = 2


def _snapshot_no_price(snap: MarketPriceSnapshot) -> float:
    return 1.0 - snap.yes_price


def _format_delta_cents(delta_cents: int) -> str:
    if delta_cents > 0:
        return f"▲ +{delta_cents}¢"
    if delta_cents < 0:
        return f"▼ {delta_cents}¢"
    return "— flat"


def _initial_open_prices(bet: Bet) -> tuple[float, float]:
    """LMSR prices at market creation (q_yes = q_no = 0)."""
    yes = lmsr_price_yes(0.0, 0.0, bet.liquidity_b)
    return yes, 1.0 - yes


def _open_prices(
    snapshots: list[MarketPriceSnapshot],
    bet: Bet,
) -> tuple[float, float]:
    """Baseline prices for 'since open' deltas."""
    if snapshots and snapshots[0].event == MarketSnapshotEvent.OPEN:
        snap = snapshots[0]
        return snap.yes_price, _snapshot_no_price(snap)
    return _initial_open_prices(bet)


def _display_price_series(
    bet: Bet,
    snapshots: list[MarketPriceSnapshot],
    yes_price: float,
    no_price: float,
) -> tuple[list[float], list[float]]:
    """Price history for charts, including live price when it differs."""
    if snapshots:
        yes_prices = [snap.yes_price for snap in snapshots]
        no_prices = [_snapshot_no_price(snap) for snap in snapshots]
    else:
        yes_prices, no_prices = list(_initial_open_prices(bet))

    if abs(yes_prices[-1] - yes_price) > 1e-9:
        yes_prices.append(yes_price)
        no_prices.append(no_price)
    return yes_prices, no_prices


def _chart_datetimes(
    snapshots: list[MarketPriceSnapshot],
    point_count: int,
) -> list[datetime]:
    times = [snap.recorded_at for snap in snapshots]
    while len(times) < point_count:
        times.append(times[-1] if times else datetime.now())
    return times[:point_count]


def _sparse_chart_labels(
    times: list[datetime],
    *,
    max_labeled: int = 6,
    pad: int = 6,
) -> list[str]:
    """Sparse weekday + time labels — one dot per trade, readable across days."""
    count = len(times)
    if count <= max_labeled:
        return [recorded_at.strftime("%a %H:%M") for recorded_at in times]
    indices = [
        int(round(index * (count - 1) / (max_labeled - 1)))
        for index in range(max_labeled)
    ]
    labels = [" " * pad for _ in range(count)]
    for index in indices:
        labels[index] = times[index].strftime("%a %H:%M")
    return labels


def _hour_chart_labels(times: list[datetime]) -> list[str]:
    return [recorded_at.strftime("%H:%M") for recorded_at in times]


def _chart_labels(
    snapshots: list[MarketPriceSnapshot],
    point_count: int,
) -> list[str]:
    return _sparse_chart_labels(_chart_datetimes(snapshots, point_count))


def _strip_ansi(text: str) -> str:
    return re.sub(r"\x1b\[[0-9;]*m", "", text)


_ANSI_RE = re.compile(r"\x1b\[([0-9;]*)m")
# plotext uses xterm 256-color codes; Discord ansi blocks only support 16 colors.
_XTERM_TO_DISCORD_FG: dict[int, str] = {
    0: "30",
    7: "30",
    8: "30",
    1: "31",
    9: "31",
    2: "32",
    10: "32",
    3: "33",
    11: "33",
    4: "34",
    12: "34",
    5: "35",
    13: "35",
    6: "36",
    14: "36",
    15: "37",
}


def _plotext_code_to_discord(code: str) -> str | None:
    if not code or code == "0":
        return "0"
    if code.startswith("48;"):
        return None
    if code.startswith("38;5;"):
        try:
            color_index = int(code.rsplit(";", 1)[-1])
        except ValueError:
            return None
        return _XTERM_TO_DISCORD_FG.get(color_index)
    return None


def _compress_ansi_escapes(text: str) -> str:
    parts = re.split(r"(\x1b\[[0-9;]*m)", text)
    compressed: list[str] = []
    previous = ""
    for part in parts:
        if part.startswith("\x1b["):
            if part == previous:
                continue
            previous = part
            compressed.append(part)
        else:
            previous = ""
            compressed.append(part)
    merged = "".join(compressed)
    return re.sub(r"\x1b\[0m(?=\x1b\[(?:3[0-7])m)", "", merged)


def prepare_plotext_chart_for_discord(text: str) -> str:
    """Map plotext's 256-color ANSI to Discord's 16-color ansi code blocks."""

    def repl(match: re.Match[str]) -> str:
        discord_code = _plotext_code_to_discord(match.group(1))
        if discord_code is None:
            return ""
        return f"\x1b[{discord_code}m"

    return _compress_ansi_escapes(_ANSI_RE.sub(repl, text))


def wrap_discord_ansi_chart(chart: str) -> str:
    return f"```ansi\n{chart}\n```"


def _downsample_dual_series(
    yes_values: list[float],
    no_values: list[float],
    labels: list[str],
    *,
    max_points: int,
) -> tuple[list[float], list[float], list[str]]:
    if len(yes_values) <= max_points:
        return yes_values, no_values, labels
    indices = [
        int(round(index * (len(yes_values) - 1) / (max_points - 1)))
        for index in range(max_points)
    ]
    return (
        [yes_values[index] for index in indices],
        [no_values[index] for index in indices],
        [labels[min(index, len(labels) - 1)] for index in indices],
    )


def render_plotext_price_chart(
    yes_values: list[float],
    no_values: list[float],
    labels: list[str],
    *,
    title: str,
    width: int = _PLOTEXT_WIDTH,
    height: int = _PLOTEXT_HEIGHT,
    max_points: int = _PLOTEXT_MAX_POINTS,
) -> str:
    """Scatter chart via plotext — one dot per snapshot, YES and NO on 0–100¢."""
    yes_values, no_values, labels = _downsample_dual_series(
        yes_values,
        no_values,
        labels,
        max_points=max_points,
    )

    plt.clf()
    plt.theme("dark")
    plt.plotsize(width, height)
    x = list(range(len(yes_values)))
    plt.yfrequency(_PLOTEXT_Y_FREQUENCY)
    plt.scatter(
        x,
        [value * 100 for value in yes_values],
        label="YES",
        color="green+",
        marker="+",
    )
    plt.scatter(
        x,
        [value * 100 for value in no_values],
        label="NO",
        color="red+",
        marker="x",
    )
    plt.ylim(0, 100)
    plt.yticks([])
    plt.title(title)
    if labels:
        plt.xticks(x, labels)
    return prepare_plotext_chart_for_discord(plt.build()).strip()


def _render_chart_for_discord(
    yes_prices: list[float],
    no_prices: list[float],
    times: list[datetime],
    *,
    title: str,
    max_points: int = _PLOTEXT_MAX_POINTS,
    max_points_fallbacks: tuple[int, ...] = (),
    sizes: tuple[tuple[int, int], ...] = (
        (_PLOTEXT_WIDTH, _PLOTEXT_HEIGHT),
        (_PLOTEXT_COMPACT_WIDTH, _PLOTEXT_COMPACT_HEIGHT),
        (_PLOTEXT_SMALL_WIDTH, _PLOTEXT_SMALL_HEIGHT),
    ),
    sparse_label_count: int = 6,
    char_budget: int = _DISCORD_FIELD_LIMIT,
    require_fit: bool = False,
) -> str | None:
    """Pick plot size and label density to fit Discord's embed field char limit."""
    point_limits = (max_points, *max_points_fallbacks)
    label_sets = (
        _sparse_chart_labels(times, max_labeled=sparse_label_count),
        _hour_chart_labels(times),
    )
    for points in point_limits:
        for labels in label_sets:
            for width, height in sizes:
                chart = render_plotext_price_chart(
                    yes_prices,
                    no_prices,
                    labels,
                    title=title,
                    width=width,
                    height=height,
                    max_points=points,
                )
                if len(wrap_discord_ansi_chart(chart)) <= char_budget:
                    return chart

    if require_fit:
        return None

    # Last resort: smallest size / fewest points, shrinking until it fits.
    width, height = sizes[-1]
    points = point_limits[-1]
    labels = _hour_chart_labels(times)
    while points >= 8:
        chart = render_plotext_price_chart(
            yes_prices,
            no_prices,
            labels,
            title=title,
            width=width,
            height=height,
            max_points=points,
        )
        if len(wrap_discord_ansi_chart(chart)) <= char_budget:
            return chart
        points -= 3
    return chart


def chart_field_length(
    yes_prices: list[float],
    no_prices: list[float],
    times: list[datetime],
    *,
    title: str,
) -> int:
    """Length of the Trend embed field value for a chart."""
    chart = _render_chart_for_discord(yes_prices, no_prices, times, title=title)
    assert chart is not None
    return len(wrap_discord_ansi_chart(chart))


def _format_chart_display(
    bet: Bet,
    snapshots: list[MarketPriceSnapshot],
    yes_price: float,
    no_price: float,
) -> str:
    """YES/NO price chart for /market_analysis."""
    yes_prices, no_prices = _display_price_series(
        bet, snapshots, yes_price, no_price
    )
    if len(yes_prices) < 2:
        return "_Trend appears after the first trade._"

    times = _chart_datetimes(snapshots, len(yes_prices))
    chart = _render_chart_for_discord(
        yes_prices,
        no_prices,
        times,
        title=f"Market #{bet.id}",
    )
    assert chart is not None
    return wrap_discord_ansi_chart(chart)


def _render_board_chart_for_discord(
    yes_prices: list[float],
    no_prices: list[float],
    times: list[datetime],
    *,
    title: str,
    char_budget: int,
) -> str:
    """Board chart: tall Y-resolved plot for light traffic, compact fallback when heavy."""
    light = _render_chart_for_discord(
        yes_prices,
        no_prices,
        times,
        title=title,
        max_points=_BOARD_PLOTEXT_MAX_POINTS,
        sizes=_BOARD_PLOTEXT_LIGHT_SIZES,
        sparse_label_count=4,
        char_budget=char_budget,
        require_fit=True,
    )
    if light is not None:
        return light

    heavy = _render_chart_for_discord(
        yes_prices,
        no_prices,
        times,
        title=title,
        max_points=_BOARD_PLOTEXT_MAX_POINTS,
        max_points_fallbacks=_BOARD_PLOTEXT_HEAVY_POINT_FALLBACKS,
        sizes=_BOARD_PLOTEXT_HEAVY_SIZES,
        sparse_label_count=4,
        char_budget=char_budget,
    )
    assert heavy is not None
    return heavy


def _format_board_chart_display(
    bet: Bet,
    snapshots: list[MarketPriceSnapshot],
    yes_price: float,
    no_price: float,
    *,
    char_budget: int = _DISCORD_FIELD_LIMIT,
) -> str | None:
    """Tall scatter chart for the live market board (Y resolution prioritized)."""
    yes_prices, no_prices = _display_price_series(
        bet, snapshots, yes_price, no_price
    )
    if len(yes_prices) < 2:
        return None

    times = _chart_datetimes(snapshots, len(yes_prices))
    chart = _render_board_chart_for_discord(
        yes_prices,
        no_prices,
        times,
        title=f"#{bet.id}",
        char_budget=char_budget,
    )
    return wrap_discord_ansi_chart(chart)


def build_market_analysis_embed(
    bet: Bet,
    snapshots: list[MarketPriceSnapshot],
    *,
    guild_id: int | None = None,
) -> discord.Embed:
    """Trading-style embed for /market_analysis with plotext price charts."""
    yes_price = lmsr_price_yes(bet.q_yes, bet.q_no, bet.liquidity_b)
    no_price = lmsr_price_no(bet.q_yes, bet.q_no, bet.liquidity_b)
    trade_count = sum(
        1
        for snap in snapshots
        if snap.event in (MarketSnapshotEvent.BUY, MarketSnapshotEvent.SELL)
    )

    open_yes, open_no = _open_prices(snapshots, bet)
    yes_delta = int(round((yes_price - open_yes) * 100))
    no_delta = int(round((no_price - open_no) * 100))

    trend_value = _format_chart_display(bet, snapshots, yes_price, no_price)

    description = (
        f"**{YES_EMOJI} YES** **{format_price_cents(yes_price)}** "
        f"({_format_delta_cents(yes_delta)} since open) · "
        f"**{NO_EMOJI} NO** **{format_price_cents(no_price)}** "
        f"({_format_delta_cents(no_delta)} since open)"
    )

    color = _embed_color(bet, yes_price)
    embed = discord.Embed(
        title=f"Market #{bet.id}",
        description=description,
        color=color,
        timestamp=snapshots[-1].recorded_at if snapshots else bet.created_at,
    )
    embed.add_field(name="Question", value=bet.question, inline=False)
    embed.add_field(name="Trend", value=trend_value, inline=False)
    embed.add_field(name="Status", value=_status_label(bet.status), inline=True)
    embed.add_field(name="Trades", value=str(trade_count), inline=True)
    embed.add_field(
        name="Closes",
        value=discord.utils.format_dt(bet.close_time, style="R"),
        inline=True,
    )

    jump_guild_id = guild_id if guild_id is not None else bet.guild_id
    if bet.message_id and jump_guild_id:
        url = (
            f"https://discord.com/channels/{jump_guild_id}/"
            f"{bet.channel_id}/{bet.message_id}"
        )
        embed.add_field(name="Market post", value=f"[Jump to market]({url})", inline=False)

    embed.set_footer(text=f"Market ID {bet.id} · LMSR liquidity {bet.liquidity_b:g}")
    return embed


def _embed_color(bet: Bet, yes_price: float) -> discord.Color:
    if bet.status == BetStatus.RESOLVED:
        return discord.Color.blue()
    if bet.status == BetStatus.CANCELLED:
        return discord.Color.dark_grey()
    if bet.status == BetStatus.CLOSED:
        return discord.Color.orange()
    if yes_price >= 0.5:
        return discord.Color.green()
    return discord.Color.red()


def _status_label(status: BetStatus) -> str:
    return {
        BetStatus.OPEN: "🟢 Open",
        BetStatus.CLOSED: "🔴 Closed (awaiting resolution)",
        BetStatus.RESOLVED: "✅ Resolved",
        BetStatus.CANCELLED: "🚫 Cancelled",
    }[status]


def _short_question(question: str, max_len: int = 42) -> str:
    text = question.strip()
    if len(text) > max_len:
        return text[: max_len - 1] + "…"
    return text


def format_market_board_meta(
    bet: Bet,
    snapshots: list[MarketPriceSnapshot],
) -> str:
    """Price / trade summary line for a board field."""
    yes_price = lmsr_price_yes(bet.q_yes, bet.q_no, bet.liquidity_b)
    no_price = lmsr_price_no(bet.q_yes, bet.q_no, bet.liquidity_b)
    trade_count = sum(
        1
        for snap in snapshots
        if snap.event in (MarketSnapshotEvent.BUY, MarketSnapshotEvent.SELL)
    )

    open_yes, open_no = _open_prices(snapshots, bet)
    yes_delta = int(round((yes_price - open_yes) * 100))
    no_delta = int(round((no_price - open_no) * 100))

    if bet.status in (BetStatus.RESOLVED, BetStatus.CANCELLED):
        timing = "resolved" if bet.status == BetStatus.RESOLVED else "cancelled"
    elif bet.status == BetStatus.CLOSED:
        timing = f"closed · {discord.utils.format_dt(bet.close_time, style='R')}"
    else:
        timing = f"closes {discord.utils.format_dt(bet.close_time, style='R')}"

    return (
        f"{YES_EMOJI} **{format_price_cents(yes_price)}** ({_format_delta_cents(yes_delta)})"
        f" · {NO_EMOJI} **{format_price_cents(no_price)}** ({_format_delta_cents(no_delta)})"
        f" · {trade_count} trades · {timing}"
    )


def _board_list_timing(bet: Bet) -> str:
    if bet.status == BetStatus.RESOLVED:
        if bet.outcome is not None:
            return f"resolved {bet.outcome.value.upper()}"
        return "resolved"
    if bet.status == BetStatus.CANCELLED:
        return "cancelled"
    if bet.status == BetStatus.CLOSED:
        return f"closed · {discord.utils.format_dt(bet.close_time, style='R')}"
    return f"closes {discord.utils.format_dt(bet.close_time, style='R')}"


def _board_market_link(bet: Bet, *, guild_id: int) -> str:
    question = _short_question(bet.question, _BOARD_LIST_QUESTION_LEN)
    if bet.message_id:
        return (
            f"[#{bet.id} {question}](https://discord.com/channels/{guild_id}/"
            f"{bet.channel_id}/{bet.message_id})"
        )
    return f"**#{bet.id}** {question}"


def format_market_board_list_line(
    bet: Bet,
    snapshots: list[MarketPriceSnapshot],
    *,
    guild_id: int,
) -> str:
    """One compact index row for the board market list."""
    yes_price = lmsr_price_yes(bet.q_yes, bet.q_no, bet.liquidity_b)
    trade_count = sum(
        1
        for snap in snapshots
        if snap.event in (MarketSnapshotEvent.BUY, MarketSnapshotEvent.SELL)
    )
    trades = "1 trade" if trade_count == 1 else f"{trade_count} trades"
    link = _board_market_link(bet, guild_id=guild_id)
    return (
        f"{link} · {YES_EMOJI} **{format_price_cents(yes_price)}**"
        f" · {trades} · {_board_list_timing(bet)}"
    )


def format_market_board_list(
    entries: list[tuple[Bet, list[MarketPriceSnapshot]]],
    *,
    guild_id: int,
) -> str:
    """Scrollable index of every market on the board."""
    if not entries:
        return ""

    lines = ["**Markets**"]
    for bet, snapshots in entries:
        lines.append(
            format_market_board_list_line(bet, snapshots, guild_id=guild_id)
        )

    omitted = 0
    while len(lines) > 1:
        text = "\n".join(lines)
        if omitted:
            suffix = f"\n_…and {omitted} more_"
            if len(text) + len(suffix) <= _DISCORD_DESCRIPTION_LIMIT:
                return text + suffix
        elif len(text) <= _DISCORD_DESCRIPTION_LIMIT:
            return text
        lines.pop()
        omitted += 1

    return lines[0]


def format_market_board_field_name(bet: Bet, *, guild_id: int) -> str:
    question = _short_question(bet.question, 40)
    if bet.message_id:
        return (
            f"[#{bet.id}](https://discord.com/channels/{guild_id}/"
            f"{bet.channel_id}/{bet.message_id}) {question}"
        )
    return f"#{bet.id} {question}"


def format_market_board_field_value(
    bet: Bet,
    snapshots: list[MarketPriceSnapshot],
) -> str:
    """Meta line plus compact scatter chart for one board market."""
    yes_price = lmsr_price_yes(bet.q_yes, bet.q_no, bet.liquidity_b)
    no_price = lmsr_price_no(bet.q_yes, bet.q_no, bet.liquidity_b)
    meta = format_market_board_meta(bet, snapshots)
    chart_budget = _DISCORD_FIELD_LIMIT - len(meta) - 1
    chart = _format_board_chart_display(
        bet,
        snapshots,
        yes_price,
        no_price,
        char_budget=chart_budget,
    )
    if chart:
        return f"{meta}\n{chart}"
    return f"{meta}\n_Trend appears after the first trade._"


_BOARD_FOOTER = "Updates on each trade · resolved markets drop off after 24h · cancelled markets removed immediately"


def build_market_board_list_embed(
    entries: list[tuple[Bet, list[MarketPriceSnapshot]]],
    *,
    guild_id: int,
) -> discord.Embed:
    """Dedicated list message for the market board channel."""
    count = len(entries)
    embed = discord.Embed(
        title=f"Live markets ({count})",
        description=format_market_board_list(entries, guild_id=guild_id),
        color=discord.Color.teal(),
    )
    embed.set_footer(text=_BOARD_FOOTER)
    return embed


def build_market_board_embeds(
    entries: list[tuple[Bet, list[MarketPriceSnapshot]]],
    *,
    guild_id: int,
) -> list[discord.Embed]:
    """Dashboard embeds with up to two tall charts per message."""
    if not entries:
        return []

    count = len(entries)
    embeds: list[discord.Embed] = []
    current: discord.Embed | None = None
    field_count = 0

    for bet, snapshots in entries:
        field_name = format_market_board_field_name(bet, guild_id=guild_id)
        field_value = format_market_board_field_value(bet, snapshots)

        need_new_page = (
            current is None
            or field_count >= _MAX_MARKETS_PER_BOARD_PAGE
        )
        if need_new_page:
            if current is not None:
                embeds.append(current)
            title = f"Live markets ({count})"
            if embeds:
                title += " — continued"
            current = discord.Embed(title=title, color=discord.Color.teal())
            field_count = 0

        current.add_field(name=field_name, value=field_value, inline=False)
        field_count += 1

    if current is not None:
        embeds.append(current)

    page_total = len(embeds)
    for page_index, embed in enumerate(embeds):
        footer = _BOARD_FOOTER
        if page_total > 1:
            footer += f" · page {page_index + 1}/{page_total}"
        embed.set_footer(text=footer)

    return embeds


def build_market_board_embed(
    entries: list[tuple[Bet, list[MarketPriceSnapshot]]],
    *,
    guild_id: int,
) -> discord.Embed:
    """First board page, or an empty-state embed when there are no markets."""
    embeds = build_market_board_embeds(entries, guild_id=guild_id)
    if embeds:
        return embeds[0]
    return discord.Embed(
        title="Live markets",
        description="No active prediction markets.",
        color=discord.Color.dark_grey(),
    )
