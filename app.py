#!/usr/bin/env python3
"""
funkSTONKS (formerly MoneyFlow) - private expense tracking, entirely local.

Sources:
  * bank CSV (George / Erste Bank export)
  * PayPal CSV (activity export)
  * Splitwise CSV (group or full export)

Core idea:
  The bank is the base. PayPal collective debits are resolved against the
  real PayPal transactions (merchant name instead of "PayPal Europe").
  Splitwise expenses correct the amount to the OWN share (effective_amount).
  Settlement payments (settle-up) do not count as expenses.

Start:  python3 app.py   ->  http://127.0.0.1:5010

Since 2026-10-14 the code lives in the package funkstonks/ (one module per
topic, see funkstonks/__init__.py). This file is only the entry point: it
starts the server and re-exports all names so that tests and scripts can
keep writing `import app as m`.
"""

import sys as _sys
import types as _types

import funkstonks as _pkg
from funkstonks import config as _config
from funkstonks.automations import (
    CARD_SETTLEMENT_WORDS,
    FOREIGN_POS_RE,
    _fmt_day,
    auto_match_card,
    auto_match_paypal,
    auto_split_wohnung_fixed,
    categorize_foreign_pos,
    detect_splitwise_anomalies,
    health_checks,
    is_paypal_clearing,
    looks_like_card_settlement,
    matched_ids,
    reconcile_fixed_cost_positions,
)
from funkstonks.backup import (
    BACKUP_NAME_RE,
    BACKUP_TAGS,
    _backups_dir,
    backup_db,
    list_backups,
    restore_backup,
    restore_from_file,
)
from funkstonks.categories import (
    _family_shades,
    _shade,
    assign_category_colors,
    category_choice_groups,
    category_context,
    category_qs,
    expense_rows,
    group_expenses_by_parent,
    top_category_names,
)
from funkstonks.config import (
    ALL_SOURCES,
    APP_DIR,
    APP_VERSION,
    DB_PATH,
    DEFAULT_CATEGORIES,
    DEFAULT_RULES,
    RES_DIR,
    SETTING_DEFAULTS,
    SPLITWISE_CAT_MAP,
    SYSTEM_CATS,
    THEME_KEYS,
    THEMES,
    TX_LIST_LIMIT,
    VARIABLE_CATS,
    BadInput,
)
from funkstonks.db import (
    db,
    disabled_automations,
    enabled_sources,
    fixed_cost_group,
    fixed_cost_like_sql,
    fixed_cost_patterns,
    fixed_cost_rows,
    fixed_cost_sw_keywords,
    get_setting,
    get_setting_list,
    income_category_name,
    init_db,
    invalidate_fixed_cost_cache,
    is_fixed_cost_group,
    like_escape,
    migrate_settings,
    migrate_settings_v2,
    parse_cutoff,
    set_setting,
    splitwise_name,
    system_cat_id,
)
from funkstonks.demo import load_demo_data, remove_demo_data
from funkstonks.depot import (
    DEPOT_COLUMNS,
    depot_hash,
    depot_positions,
    depot_series_by_month,
    depot_summary,
    depot_value_by_month,
    import_depot,
)
from funkstonks.importers import (
    CARD_SKIP_DESC,
    PAYPAL_SKIP_TYPES,
    cleanup_before_start,
    demote_splitwise_position,
    import_bank,
    import_card,
    import_paypal,
    import_splitwise,
    is_auto_promoted,
    is_fixed_cost_position,
    is_shared_fixed_cost_position,
    sync_splitwise_group,
    tracking_start,
    unlink_splitwise_position,
)
from funkstonks.parsing import (
    _XLSX_NS,
    BANK_COLUMNS,
    EXPORT_HEADER,
    IMPORT_KIND_LABEL,
    _depot_number,
    _xlsx_col_index,
    bank_columns,
    detect_kind,
    find_col,
    header_signature,
    is_card_account,
    is_own_export,
    parse_amount,
    parse_date,
    read_csv_flexible,
    read_xlsx_rows,
)
from funkstonks.periods import (
    MONTH_NAMES,
    MONTH_RE,
    _dashboard_period,
    _period_name,
    months_between,
    period_args,
    period_label,
    previous_period,
    valid_month,
    wohnung_fixed_display_months,
)
from funkstonks.quotes import (
    CHART_URL,
    EUR_SUFFIXES,
    SEARCH_URL,
    TIMEOUT,
    eur_rate,
    fetch_prices,
    http_get,
    maybe_refresh_prices,
    quote,
    search_symbol,
    upsert_price,
)
from funkstonks.recurring import (
    SERIES_STOPWORDS,
    _forced_entry,
    _gaps_fit,
    _median,
    _merge_related_series,
    _norm_partner,
    _one_per_month,
    _rejected_group,
    _series_ended,
    _series_entry,
    _series_repayments,
    _series_senders,
    _series_skipped,
    _series_term,
    _series_tokens,
    detect_recurring,
    recurring_groups,
)
from funkstonks.repay import (
    REPAY_NOTE,
    auto_link_repayments,
    is_repayment_linked,
    link_repayment,
    repayment_candidates_sql,
    unlink_repayment,
)
from funkstonks.rules import (
    _RULE_HITS,
    URLAUB_PROTECTED_SQL,
    apply_rules,
    hash_with_occurrence,
    income_rule_patterns,
    is_splitwise_settlement,
    map_splitwise_category,
    normalize_pattern,
    rule_suggestion_for,
    rule_target_txs,
    set_income_rules,
    splitwise_category_name,
    tx_hash,
    upsert_rule,
)
from funkstonks.share import (
    PUBLIC_BRANCH,
    PUBLIC_COMMIT_SUBJECT,
    RELEASE_NOTES,
    RELEASE_ZIP,
    SHARE_EXCLUDE,
    SHARE_INCLUDE,
    build_release,
    build_share_tree,
    is_dev_checkout,
    publish_share,
)
from funkstonks.splitwise import (
    SW_AMOUNT_TOLERANCE,
    SW_AMOUNT_WINDOW_DAYS,
    auto_link_own_advances,
    auto_umbuchung_splitwise_settlements,
    find_splitwise_settlement,
    link_own_advance,
    promote_own_advance_credits,
    promote_splitwise_shares,
    save_splits,
    settle_composition,
    splitwise_amount_mismatches,
    splitwise_overview,
    sw_fixed_to_partner,
)
from funkstonks.stats import (
    HUE_AUSGABEN,
    HUE_BLUE,
    HUE_GREEN,
    HUE_ORANGE,
    HUE_RED,
    _period_totals,
    _smoothed_transactions,
    build_sankey_nodes,
    compare_periods,
    dashboard_month_transactions,
    dashboard_period_transactions,
    eff,
    hidden_positive_categories,
    stats_transactions,
    summarize,
    todo_count,
    year_matrix,
    year_matrix_groups,
    year_transactions,
)
from funkstonks.views_dashboard import (
    _dashboard_context,
    _investitionen_context,
    api_investitionen,
    api_period,
    dashboard,
    investitionen_kurs_manuell,
    investitionen_kurse,
    investitionen_page,
    partner_page,
    vergleich_page,
    year_page,
)
from funkstonks.views_fixkosten import fixkosten, fixkosten_override, fixkosten_serie, fixkosten_serie_skip
from funkstonks.views_import import (
    _stash_upload,
    _uploads_dir,
    bank_column_maps,
    import_colmap,
    import_page,
    run_import,
)
from funkstonks.views_manage import (
    categories_page,
    demo_page,
    help_page,
    rule_apply,
    rules_page,
    settings_page,
    setup_page,
    sicherung_download,
    sicherung_page,
)
from funkstonks.views_splitwise import (
    _back_to_group,
    splitwise_amount_ok,
    splitwise_close_group,
    splitwise_dismiss,
    splitwise_link,
    splitwise_page,
    splitwise_toggle_urlaub,
    splitwise_undismiss,
    splitwise_unlink,
)
from funkstonks.views_transactions import (
    card_page,
    export_csv,
    paypal_page,
    repay_page,
    repay_unlink,
    review,
    split_page,
    transactions,
    tx_update,
)
from funkstonks.web import (
    SETUP_EXEMPT,
    _back,
    _bad_input,
    _close_leftover_connections,
    _integrity,
    _value_error,
    app,
    csrf_guard,
    form_category_id,
    form_int,
    inject_globals,
    load_secret_key,
    local_path,
    main,
    require_setup,
)


class _EntryModule(_types.ModuleType):
    """`m.DB_PATH = ...` in tests has to reach the package - db() reads
    config.DB_PATH, not the copy here."""

    def __setattr__(self, name, value):
        if name == "DB_PATH":
            _config.DB_PATH = value
        super().__setattr__(name, value)


_sys.modules[__name__].__class__ = _EntryModule

if __name__ == "__main__":
    main()
