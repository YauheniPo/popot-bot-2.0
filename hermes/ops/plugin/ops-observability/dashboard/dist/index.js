(function () {
  "use strict";

  var SDK = window.__HERMES_PLUGIN_SDK__;
  if (!SDK || !window.__HERMES_PLUGINS__) return;

  var React = SDK.React;
  var h = React.createElement;
  var PERIODS = ["24h", "7d", "30d"];

  function formatNumber(value) {
    return new Intl.NumberFormat().format(Number(value || 0));
  }

  function formatCost(value) {
    return "$" + Number(value || 0).toFixed(4);
  }

  function api(period) {
    return SDK.fetchJSON("/api/plugins/ops-observability/summary?period=" + encodeURIComponent(period));
  }

  function MetricCard(props) {
    return h("section", { className: "om-card" }, [
      h("div", { className: "om-label", key: "label" }, props.label),
      h("div", { className: "om-value", key: "value" }, props.value),
      props.detail ? h("div", { className: "om-detail", key: "detail" }, props.detail) : null
    ]);
  }

  function Rows(props) {
    if (!props.items || props.items.length === 0) {
      return h("p", { className: "om-empty" }, "Нет данных за выбранный период.");
    }
    var maximum = Math.max.apply(null, props.items.map(function (item) { return Number(item.calls || 0); }));
    return h("div", { className: "om-rows" }, props.items.map(function (item, index) {
      var calls = Number(item.calls || 0);
      var width = maximum ? Math.max(3, Math.round(calls * 100 / maximum)) : 3;
      return h("div", { className: "om-row", key: props.keyFor(item, index) }, [
        h("div", { className: "om-row-head", key: "head" }, [
          h("span", { className: "om-row-name", key: "name", title: props.nameFor(item) }, props.nameFor(item)),
          h("span", { className: "om-row-count", key: "count" }, formatNumber(calls))
        ]),
        h("div", { className: "om-bar", key: "bar" }, h("span", { style: { width: width + "%" } })),
        h("div", { className: "om-row-meta", key: "meta" }, props.metaFor(item))
      ]);
    }));
  }

  function MetricsPage() {
    var state = React.useState("7d");
    var period = state[0];
    var setPeriod = state[1];
    var dataState = React.useState(null);
    var data = dataState[0];
    var setData = dataState[1];
    var errorState = React.useState("");
    var error = errorState[0];
    var setError = errorState[1];
    var loadingState = React.useState(true);
    var loading = loadingState[0];
    var setLoading = loadingState[1];

    var refresh = React.useCallback(function () {
      setLoading(true);
      api(period).then(function (next) {
        setData(next);
        setError("");
      }).catch(function (reason) {
        setError("Не удалось загрузить метрики: " + String(reason && reason.message || reason));
      }).finally(function () {
        setLoading(false);
      });
    }, [period]);

    React.useEffect(function () {
      refresh();
      var timer = window.setInterval(refresh, 30000);
      return function () { window.clearInterval(timer); };
    }, [refresh]);

    var summary = data && data.summary || {};
    var timeline = data && data.timeline || [];
    var maxTimeline = Math.max.apply(null, [1].concat(timeline.map(function (item) { return Number(item.calls || 0); })));
    var health = data && data.health || {};

    return h("main", { className: "om-page" }, [
      h("header", { className: "om-header", key: "header" }, [
        h("div", { key: "title" }, [
          h("h1", { key: "h1" }, "Metrics"),
          h("p", { key: "p" }, "Read-only observability data collected locally by Hermes.")
        ]),
        h("div", { className: "om-actions", key: "actions" }, [
          h("div", { className: "om-periods", key: "periods" }, PERIODS.map(function (value) {
            return h("button", {
              className: value === period ? "om-period is-active" : "om-period",
              key: value,
              onClick: function () { setPeriod(value); }
            }, value);
          })),
          h("button", { className: "om-refresh", key: "refresh", onClick: refresh, disabled: loading }, loading ? "Loading…" : "Refresh")
        ])
      ]),
      error ? h("p", { className: "om-error", key: "error" }, error) : null,
      data && !data.available ? h("section", { className: "om-notice", key: "empty" }, "Metrics database has not been created yet. Send a message to Hermes and refresh this page.") : null,
      h("section", { className: "om-cards", key: "cards" }, [
        h(MetricCard, { key: "api", label: "API calls", value: formatNumber(summary.api_calls), detail: formatNumber(summary.api_errors) + " errors" }),
        h(MetricCard, { key: "tokens", label: "Tokens", value: formatNumber(summary.tokens), detail: formatNumber(summary.input_tokens) + " input · " + formatNumber(summary.output_tokens) + " output" }),
        h(MetricCard, { key: "cost", label: "Cost", value: formatCost(summary.cost_usd), detail: "provider or configured price file" }),
        h(MetricCard, { key: "tools", label: "Tool calls", value: formatNumber(summary.tool_calls), detail: formatNumber(summary.tool_errors) + " errors" })
      ]),
      h("section", { className: "om-panel", key: "timeline" }, [
        h("div", { className: "om-panel-title", key: "title" }, "API calls over time"),
        timeline.length ? h("div", { className: "om-timeline", key: "chart" }, timeline.map(function (item) {
          var height = Math.max(4, Math.round(Number(item.calls || 0) * 100 / maxTimeline));
          return h("div", { className: "om-tick", key: item.bucket, title: item.bucket + ": " + item.calls + " calls" }, [
            h("span", { className: "om-tick-bar", key: "bar", style: { height: height + "%" } }),
            h("span", { className: "om-tick-label", key: "label" }, String(item.bucket).slice(5, 13))
          ]);
        })) : h("p", { className: "om-empty", key: "empty" }, "No API calls yet."),
        h("p", { className: "om-muted", key: "note" }, "Updated: " + (data && data.generated_at || "—"))
      ]),
      h("section", { className: "om-grid", key: "grid" }, [
        h("section", { className: "om-panel", key: "models" }, [
          h("div", { className: "om-panel-title", key: "title" }, "Models"),
          h(Rows, { key: "rows", items: data && data.models || [], keyFor: function (item, index) { return item.provider + "/" + item.model + index; }, nameFor: function (item) { return item.provider + "/" + item.model; }, metaFor: function (item) { return formatNumber(item.tokens) + " tokens · " + formatCost(item.cost_usd) + " · " + formatNumber(item.errors) + " errors"; } })
        ]),
        h("section", { className: "om-panel", key: "tools" }, [
          h("div", { className: "om-panel-title", key: "title" }, "Tools"),
          h(Rows, { key: "rows", items: data && data.tools || [], keyFor: function (item, index) { return item.name + index; }, nameFor: function (item) { return item.name; }, metaFor: function (item) { return Math.round(Number(item.avg_duration_ms || 0)) + " ms avg · " + formatNumber(item.errors) + " errors"; } })
        ])
      ]),
      h("section", { className: health.issues && health.issues.length ? "om-health om-health-bad" : "om-health", key: "health" }, [
        h("strong", { key: "title" }, health.issues && health.issues.length ? "Health requires attention" : "Health: OK"),
        h("span", { key: "detail" }, health.issues && health.issues.length ? health.issues.join(", ") : "Metrics database: " + formatNumber(health.database_bytes) + " bytes")
      ])
    ]);
  }

  window.__HERMES_PLUGINS__.register("ops-observability", MetricsPage);
}());
