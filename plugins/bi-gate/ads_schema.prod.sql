-- 从生产 ads 层导出的建表语句（只有结构与注释，**没有任何数据**）。
-- 2026-08-28 用 SHOW CREATE TABLE 导出，供在 dev 上复刻一套开发用的表。
-- 复刻的目的是拿到口径注释 —— 指标注册表从这些注释生成，不手写。
-- 属性里的副本数等按 dev 规模由 make_dev_replica.py 改写，这里保持原样便于比对。

-- ═══ ads_asset_daily_stat_di ═══
CREATE TABLE `ads_asset_daily_stat_di` (
  `bizdate` date NOT NULL COMMENT "日期",
  `asset_balance_gt_0_uv` bigint(20) NOT NULL COMMENT "资产余额大于0的用户数",
  `total_asset_balance` decimal(38, 8) NOT NULL COMMENT "资产总额",
  `etl_time` datetime NOT NULL COMMENT "数仓ETL时间"
) ENGINE=OLAP 
PRIMARY KEY(`bizdate`)
COMMENT "全站资沉统计"
DISTRIBUTED BY HASH(`bizdate`)
PROPERTIES (
"compression" = "LZ4",
"enable_persistent_index" = "true",
"fast_schema_evolution" = "true",
"replicated_storage" = "true",
"replication_num" = "3"
);

-- ═══ ads_bd_refer_trade_daily_stat_di ═══
CREATE TABLE `ads_bd_refer_trade_daily_stat_di` (
  `bizdate` date NOT NULL COMMENT "日期",
  `bd_name` varchar(256) NOT NULL COMMENT "BD名称",
  `register_uv` bigint(20) NOT NULL COMMENT "注册人数",
  `FTD` bigint(20) NOT NULL COMMENT "首次入金人数",
  `DAUC` bigint(20) NOT NULL COMMENT "合约交易人数",
  `FTTC` bigint(20) NOT NULL COMMENT "首次合约交易人数",
  `eFTTC` bigint(20) NOT NULL COMMENT "有效首次合约交易人数",
  `trade_amount_u` decimal(38, 8) NOT NULL COMMENT "合约交易金额",
  `trade_fee_u` decimal(38, 8) NOT NULL COMMENT "合约交易手续费",
  `commission_u` decimal(38, 8) NOT NULL COMMENT "合约交易返佣金额",
  `risk_commission_u` decimal(38, 8) NOT NULL COMMENT "合约交易被风控的返佣金额",
  `net_fee_u` decimal(38, 8) NOT NULL COMMENT "留存手续费",
  `deposit` decimal(38, 8) NOT NULL COMMENT "入金金额",
  `withdrawal` decimal(38, 8) NOT NULL COMMENT "出金金额",
  `net_deposit` decimal(38, 8) NOT NULL COMMENT "净入金",
  `etl_time` datetime NOT NULL COMMENT "数仓ETL时间"
) ENGINE=OLAP 
PRIMARY KEY(`bizdate`, `bd_name`)
COMMENT "BD拉新、交易、返佣、出金日报"
DISTRIBUTED BY HASH(`bizdate`)
PROPERTIES (
"compression" = "LZ4",
"enable_persistent_index" = "true",
"fast_schema_evolution" = "true",
"replicated_storage" = "true",
"replication_num" = "3"
);

-- ═══ ads_contract_order_deal_stat_ha ═══
CREATE TABLE `ads_contract_order_deal_stat_ha` (
  `part_hour` datetime NOT NULL COMMENT "截止时间",
  `contract_id` int(11) NOT NULL COMMENT "合约ID",
  `contract_name` varchar(512) NOT NULL COMMENT "合约名称",
  `order_cnt` bigint(20) NOT NULL COMMENT "历史订单数",
  `finish_order_cnt` bigint(20) NOT NULL COMMENT "已成交订单数",
  `progress_order_cnt` bigint(20) NOT NULL COMMENT "未成交订单数",
  `progress_order_size` decimal(38, 8) NOT NULL COMMENT "未成交订单量",
  `history_deal_order_cnt` bigint(20) NOT NULL COMMENT "近72小时历史成交数",
  `etl_time` datetime NOT NULL COMMENT "数仓ETL时间"
) ENGINE=OLAP 
PRIMARY KEY(`part_hour`, `contract_id`, `contract_name`)
COMMENT "合约订单统计"
DISTRIBUTED BY HASH(`contract_id`)
PROPERTIES (
"compression" = "LZ4",
"enable_persistent_index" = "true",
"fast_schema_evolution" = "true",
"replicated_storage" = "true",
"replication_num" = "3"
);

-- ═══ ads_contract_trade_daily_stat_di ═══
CREATE TABLE `ads_contract_trade_daily_stat_di` (
  `bizdate` date NOT NULL COMMENT "日期 UTC8",
  `contract_name` varchar(512) NOT NULL COMMENT "合约名称",
  `fttc` bigint(20) NOT NULL COMMENT "首次合约交易人数(当日首次交易该合约的用户数)",
  `trade_amount_u` decimal(38, 8) NOT NULL COMMENT "合约交易金额 U本位",
  `trade_fee_u` decimal(38, 8) NOT NULL COMMENT "合约交易手续费 U本位(含强平手续费)",
  `dauc` bigint(20) NOT NULL COMMENT "合约交易人数(当日交易该合约的去重用户数)",
  `etl_time` datetime NOT NULL COMMENT "数仓ETL时间"
) ENGINE=OLAP 
PRIMARY KEY(`bizdate`, `contract_name`)
COMMENT "合约交易日报(日 + 合约维度)"
DISTRIBUTED BY HASH(`bizdate`)
PROPERTIES (
"compression" = "LZ4",
"enable_persistent_index" = "true",
"fast_schema_evolution" = "true",
"replicated_storage" = "true",
"replication_num" = "3"
);

-- ═══ ads_liquidity_contract_daily_kline_price_di ═══
CREATE TABLE `ads_liquidity_contract_daily_kline_price_di` (
  `bizdate` date NOT NULL COMMENT "日期 UTC8",
  `contract_id` int(11) NOT NULL COMMENT "合约ID",
  `contract_name` varchar(512) NOT NULL COMMENT "合约名称",
  `open_price_00` decimal(38, 8) NOT NULL COMMENT "当日开盘价(UTC8当日首根K线开盘价)",
  `prev_open_price_00` decimal(38, 8) NOT NULL COMMENT "前一日开盘价",
  `open_price_dod_rate` double NULL COMMENT "开盘价日环比",
  `open_price_std` double NULL COMMENT "日内开盘价标准差(总体, 1分钟K线)",
  `open_price_gap` decimal(38, 8) NOT NULL COMMENT "日内开盘价极差(最高开盘价-最低开盘价)",
  `etl_time` datetime NOT NULL COMMENT "数仓ETL时间"
) ENGINE=OLAP 
PRIMARY KEY(`bizdate`, `contract_id`)
COMMENT "合约流动性开盘价日报(日 + 合约)"
PARTITION BY date_trunc('month', `bizdate`)
DISTRIBUTED BY HASH(`contract_id`)
PROPERTIES (
"compression" = "LZ4",
"enable_persistent_index" = "true",
"fast_schema_evolution" = "true",
"replicated_storage" = "true",
"replication_num" = "3"
);

-- ═══ ads_liquidity_spot_daily_kline_price_di ═══
CREATE TABLE `ads_liquidity_spot_daily_kline_price_di` (
  `bizdate` date NOT NULL COMMENT "日期 UTC8",
  `symbol_id` int(11) NOT NULL COMMENT "现货ID",
  `symbol_name` varchar(512) NOT NULL COMMENT "现货名称",
  `open_price_00` decimal(38, 8) NOT NULL COMMENT "当日开盘价(UTC8当日首根K线开盘价)",
  `prev_open_price_00` decimal(38, 8) NOT NULL COMMENT "前一日开盘价",
  `open_price_dod_rate` double NULL COMMENT "开盘价日环比",
  `open_price_std` double NULL COMMENT "日内开盘价标准差(总体, 1分钟K线)",
  `open_price_gap` decimal(38, 8) NOT NULL COMMENT "日内开盘价极差(最高开盘价-最低开盘价)",
  `etl_time` datetime NOT NULL COMMENT "数仓ETL时间"
) ENGINE=OLAP 
PRIMARY KEY(`bizdate`, `symbol_id`)
COMMENT "现货流动性开盘价日报(日 + 现货)"
PARTITION BY date_trunc('month', `bizdate`)
DISTRIBUTED BY HASH(`symbol_id`)
PROPERTIES (
"compression" = "LZ4",
"enable_persistent_index" = "true",
"fast_schema_evolution" = "true",
"replicated_storage" = "true",
"replication_num" = "3"
);

-- ═══ ads_overall_daily_stat_di ═══
CREATE TABLE `ads_overall_daily_stat_di` (
  `bizdate` date NOT NULL COMMENT "日期",
  `dnu` bigint(20) NOT NULL COMMENT "注册人数",
  `dau` bigint(20) NOT NULL COMMENT "活跃人数 现货+合约+外汇人数去重",
  `daus` bigint(20) NOT NULL COMMENT "现货活跃人数",
  `dauc` bigint(20) NOT NULL COMMENT "合约活跃人数",
  `dauf` bigint(20) NOT NULL COMMENT "外汇活跃人数",
  `ftt` bigint(20) NOT NULL COMMENT "首次交易人数",
  `ftts` bigint(20) NOT NULL COMMENT "首次现货交易人数",
  `fttc` bigint(20) NOT NULL COMMENT "首次合约交易人数",
  `fttf` bigint(20) NOT NULL COMMENT "首次外汇交易人数",
  `eFTTC` bigint(20) NOT NULL COMMENT "有效首次合约交易人数",
  `spot_trade_amt_u` decimal(38, 8) NOT NULL COMMENT "现货交易金额",
  `spot_trade_fee_u` decimal(38, 8) NOT NULL COMMENT "现货交易手续费",
  `contract_trade_amt_u` decimal(38, 8) NOT NULL COMMENT "合约交易金额",
  `contract_trade_fee_u` decimal(38, 8) NOT NULL COMMENT "合约交易手续费",
  `forex_trade_volume` decimal(38, 8) NOT NULL COMMENT "外汇交易手数",
  `forex_trade_fee_u` decimal(38, 8) NOT NULL COMMENT "外汇交易手续费",
  `ftd` bigint(20) NOT NULL COMMENT "首次入金人数",
  `deposit` decimal(38, 8) NOT NULL COMMENT "入金金额",
  `withdrawal` decimal(38, 8) NOT NULL COMMENT "出金金额",
  `net_deposit` decimal(38, 8) NOT NULL COMMENT "净入金",
  `etl_time` datetime NOT NULL COMMENT "数仓ETL时间"
) ENGINE=OLAP 
PRIMARY KEY(`bizdate`)
COMMENT "全站整体日报"
DISTRIBUTED BY HASH(`bizdate`)
PROPERTIES (
"compression" = "LZ4",
"enable_persistent_index" = "true",
"fast_schema_evolution" = "true",
"replicated_storage" = "true",
"replication_num" = "3"
);

-- ═══ ads_polymarket_push_daily_report_di ═══
CREATE TABLE `ads_polymarket_push_daily_report_di` (
  `bizdate` date NOT NULL COMMENT "日期",
  `trade_users` bigint(20) NOT NULL COMMENT "交易用户数",
  `trade_amount` decimal(38, 8) NOT NULL COMMENT "交易金额 - usdc",
  `trade_fee` decimal(38, 8) NOT NULL COMMENT "交易手续费 - usdc",
  `etl_time` datetime NOT NULL COMMENT "数仓ETL时间"
) ENGINE=OLAP 
PRIMARY KEY(`bizdate`)
COMMENT "预测市场推送日报"
DISTRIBUTED BY HASH(`bizdate`)
PROPERTIES (
"compression" = "LZ4",
"enable_persistent_index" = "true",
"fast_schema_evolution" = "true",
"replicated_storage" = "true",
"replication_num" = "3"
);

-- ═══ ads_polymarket_push_monthly_report_di ═══
CREATE TABLE `ads_polymarket_push_monthly_report_di` (
  `bizdate` date NOT NULL COMMENT "日期",
  `trade_users` bigint(20) NOT NULL COMMENT "交易用户数",
  `trade_amount` decimal(38, 8) NOT NULL COMMENT "交易金额 - usdc",
  `trade_fee` decimal(38, 8) NOT NULL COMMENT "交易手续费 - usdc",
  `etl_time` datetime NOT NULL COMMENT "数仓ETL时间"
) ENGINE=OLAP 
PRIMARY KEY(`bizdate`)
COMMENT "预测市场推送周报"
DISTRIBUTED BY HASH(`bizdate`)
PROPERTIES (
"compression" = "LZ4",
"enable_persistent_index" = "true",
"fast_schema_evolution" = "true",
"replicated_storage" = "true",
"replication_num" = "3"
);

-- ═══ ads_polymarket_push_weekly_report_di ═══
CREATE TABLE `ads_polymarket_push_weekly_report_di` (
  `bizdate` date NOT NULL COMMENT "日期",
  `trade_users` bigint(20) NOT NULL COMMENT "交易用户数",
  `trade_amount` decimal(38, 8) NOT NULL COMMENT "交易金额 - usdc",
  `trade_fee` decimal(38, 8) NOT NULL COMMENT "交易手续费 - usdc",
  `etl_time` datetime NOT NULL COMMENT "数仓ETL时间"
) ENGINE=OLAP 
PRIMARY KEY(`bizdate`)
COMMENT "预测市场推送周报"
DISTRIBUTED BY HASH(`bizdate`)
PROPERTIES (
"compression" = "LZ4",
"enable_persistent_index" = "true",
"fast_schema_evolution" = "true",
"replicated_storage" = "true",
"replication_num" = "3"
);

-- ═══ ads_spot_order_deal_stat_ha ═══
CREATE TABLE `ads_spot_order_deal_stat_ha` (
  `part_hour` datetime NOT NULL COMMENT "截止时间",
  `symbol_id` int(11) NOT NULL COMMENT "现货币对ID",
  `symbol_name` varchar(512) NOT NULL COMMENT "现货币对",
  `order_cnt` bigint(20) NOT NULL COMMENT "历史订单数",
  `finish_order_cnt` bigint(20) NOT NULL COMMENT "已成交订单数",
  `progress_order_cnt` bigint(20) NOT NULL COMMENT "未成交订单数",
  `progress_order_size` decimal(38, 8) NOT NULL COMMENT "未成交订单量",
  `history_deal_order_cnt` bigint(20) NOT NULL COMMENT "近72小时历史成交数",
  `etl_time` datetime NOT NULL COMMENT "数仓ETL时间"
) ENGINE=OLAP 
PRIMARY KEY(`part_hour`, `symbol_id`, `symbol_name`)
COMMENT "现货订单统计"
DISTRIBUTED BY HASH(`symbol_id`)
PROPERTIES (
"compression" = "LZ4",
"enable_persistent_index" = "true",
"fast_schema_evolution" = "true",
"replicated_storage" = "true",
"replication_num" = "3"
);

-- ═══ ads_trade_fee_daily_report_di ═══
CREATE TABLE `ads_trade_fee_daily_report_di` (
  `bizdate` date NOT NULL COMMENT "日期",
  `trade_type` varchar(256) NOT NULL COMMENT "交易类型",
  `fee_coin` varchar(256) NOT NULL COMMENT "收取的手续费币种",
  `trade_fee` decimal(38, 8) NOT NULL COMMENT "交易手续费",
  `trade_fee_u` decimal(38, 8) NULL COMMENT "交易手续费U",
  `etl_time` datetime NOT NULL COMMENT "数仓ETL时间"
) ENGINE=OLAP 
PRIMARY KEY(`bizdate`, `trade_type`, `fee_coin`)
COMMENT "手续费收取日报"
DISTRIBUTED BY HASH(`bizdate`)
PROPERTIES (
"compression" = "LZ4",
"enable_persistent_index" = "true",
"fast_schema_evolution" = "true",
"replicated_storage" = "true",
"replication_num" = "3"
);

-- ═══ ads_world_cup_activity_user_trade_di ═══
CREATE TABLE `ads_world_cup_activity_user_trade_di` (
  `bizdate` date NOT NULL COMMENT "日期",
  `trade_users_mt5` bigint(20) NOT NULL COMMENT "MT5交易人数",
  `trade_volume_mt5` decimal(38, 8) NOT NULL COMMENT "MT5交易手数",
  `trade_users_tradfi` bigint(20) NOT NULL COMMENT "TradFi交易人数",
  `trade_volume_tradfi` decimal(38, 8) NOT NULL COMMENT "TradFi交易手数",
  `trade_users_contract` bigint(20) NOT NULL COMMENT "合约交易人数",
  `trade_amount_contract` decimal(38, 8) NOT NULL COMMENT "合约交易金额",
  `trade_users_spot` bigint(20) NOT NULL COMMENT "现货交易人数",
  `trade_amount_spot` decimal(38, 8) NOT NULL COMMENT "现货交易金额",
  `trade_users_polymarket` bigint(20) NOT NULL COMMENT "预测市场交易人数",
  `trade_amount_polymarket` decimal(38, 8) NOT NULL COMMENT "预测市场交易金额",
  `etl_time` datetime NOT NULL COMMENT "数仓ETL时间"
) ENGINE=OLAP 
PRIMARY KEY(`bizdate`)
COMMENT "世界杯活动用户交易情况"
DISTRIBUTED BY HASH(`bizdate`)
PROPERTIES (
"compression" = "LZ4",
"enable_persistent_index" = "true",
"fast_schema_evolution" = "true",
"replicated_storage" = "true",
"replication_num" = "3"
);
