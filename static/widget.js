/**
 * Craig Widget — Just-Print quoting assistant.
 *
 * Usage:
 *   <script src="https://your-domain.com/static/widget.js" defer></script>
 *
 * The script self-mounts a floating chat bubble bottom-right on any page.
 * Clicking it opens a panel with two tabs:
 *   - "Chat with Craig"  — natural conversation (DeepSeek-powered)
 *   - "Quick Quote"      — structured form (product → qty → specs → price)
 *
 * Matches just-print.ie brand: #040f2a navy, tiger logo, Poppins font,
 * rainbow accents (pink/yellow/blue/lime), fade-in-up animations,
 * pulsing bubble echoing their phone-ring pattern.
 */
(function () {
    'use strict';

    // Resolve API base from the script src so the widget works when embedded elsewhere
    const currentScript = document.currentScript || (function () {
        const scripts = document.getElementsByTagName('script');
        return scripts[scripts.length - 1];
    })();
    const scriptSrc = currentScript ? currentScript.src : '';
    const API_BASE = scriptSrc
        ? scriptSrc.replace(/\/(static\/)?widget\.js.*$/, '')
        : window.location.origin;

    // Which client (= Craig tenant) does this widget belong to?
    // Embed with: <script src=".../widget.js" data-client="just-print" defer></script>
    // Falls back to "just-print" for backwards compatibility.
    const CLIENT_SLUG =
        (currentScript && currentScript.getAttribute('data-client')) ||
        (typeof window !== 'undefined' && window.__JP_CLIENT_SLUG) ||
        'just-print';

    // Tenant branding + greeting, fetched from the server on mount.
    // Populated by bootConfig() below; has sensible defaults so the widget
    // still works if the fetch fails (e.g. offline preview).
    let WIDGET_CONFIG = {
        organization_slug: CLIENT_SLUG,
        primary_color: '#040f2a',
        logo_url: null,
        font: 'Poppins',
        greeting: 'Hey \u2014 Craig here. What are you looking to print?',
        // V5: dynamic-length accent array + stripe render mode.
        accents: ['#e30686', '#feea03', '#3e8fcd', '#040f2a'],
        stripe_mode: 'sections', // 'sections' | 'gradient' | 'solid'
        // Legacy fields kept so older backends still work unchanged.
        accent_pink: '#e30686',
        accent_yellow: '#feea03',
        accent_blue: '#3e8fcd',
    };

    /** Build a CSS `background` value for the rainbow stripe. */
    function buildStripeBackground(accents, mode, primary) {
        var colors = (accents && accents.length) ? accents.slice() : [];
        if (!colors.length) colors = [primary || '#040f2a'];
        if (mode === 'solid') {
            return colors[0];
        }
        if (mode === 'gradient') {
            if (colors.length === 1) return colors[0];
            return 'linear-gradient(90deg, ' + colors.join(', ') + ')';
        }
        // sections (default): equal-width solid bands
        if (colors.length === 1) return colors[0];
        var step = 100 / colors.length;
        var stops = colors.map(function (c, i) {
            var start = (i * step).toFixed(4);
            var end = ((i + 1) * step).toFixed(4);
            return c + ' ' + start + '% ' + end + '%';
        });
        return 'linear-gradient(90deg, ' + stops.join(', ') + ')';
    }

    async function fetchWidgetConfig() {
        try {
            const res = await fetch(
                API_BASE + '/widget-config?client=' + encodeURIComponent(CLIENT_SLUG),
                { cache: 'no-store' },
            );
            if (res.ok) {
                const data = await res.json();
                WIDGET_CONFIG = Object.assign({}, WIDGET_CONFIG, data);
                // Backwards-compat: older backends don't return `accents` — in
                // that case synthesize the array from legacy fields so the new
                // stripe renderer still has something to work with.
                if (!Array.isArray(data.accents) || data.accents.length === 0) {
                    WIDGET_CONFIG.accents = [
                        WIDGET_CONFIG.accent_pink,
                        WIDGET_CONFIG.accent_yellow,
                        WIDGET_CONFIG.accent_blue,
                        WIDGET_CONFIG.primary_color,
                    ];
                }
                if (!WIDGET_CONFIG.stripe_mode) WIDGET_CONFIG.stripe_mode = 'sections';
            }
        } catch (e) {
            console.warn('[Craig widget] /widget-config failed, using defaults:', e);
        }
    }

    // ======================================================================
    // STYLES
    // ======================================================================
    const STYLES = `
        .jp-widget {
            position: fixed;
            bottom: 0;
            right: 0;
            z-index: 999999;
            font-family: 'Poppins', 'Roboto', 'Helvetica Neue', Helvetica, Arial, sans-serif;
            color: #040f2a;
        }

        .jp-widget *, .jp-widget *::before, .jp-widget *::after {
            box-sizing: border-box;
        }

        /* ===== Floating bubble (closed state) ===== */
        .jp-bubble {
            position: fixed;
            bottom: 24px;
            right: 24px;
            width: 64px;
            height: 64px;
            border-radius: 50%;
            background: #040f2a;
            cursor: pointer;
            box-shadow: 0 10px 30px rgba(4,15,42,0.35);
            display: flex;
            align-items: center;
            justify-content: center;
            overflow: hidden;
            border: none;
            padding: 0;
            transition: transform 0.3s cubic-bezier(.68,-.01,.36,1), box-shadow 0.3s ease;
        }

        .jp-bubble:hover {
            transform: scale(1.08);
            box-shadow: 0 15px 40px rgba(4,15,42,0.5);
        }

        .jp-bubble img {
            width: 100%;
            height: 100%;
            object-fit: cover;
        }

        /* Pulsing rings — echoes their phonering-alo-circle-anim */
        .jp-bubble::before,
        .jp-bubble::after {
            content: "";
            position: absolute;
            inset: -8px;
            border-radius: 50%;
            border: 2px solid rgba(227,6,134,0.55);
            animation: jp-ring 2s infinite cubic-bezier(.25,.1,.25,1);
            pointer-events: none;
        }
        .jp-bubble::after {
            border-color: rgba(254,234,3,0.5);
            animation-delay: 1s;
        }
        @keyframes jp-ring {
            0%   { transform: scale(0.9); opacity: 0.8; }
            70%  { transform: scale(1.4); opacity: 0; }
            100% { transform: scale(1.4); opacity: 0; }
        }

        .jp-bubble.jp-hidden { display: none; }

        /* New-message ping */
        .jp-bubble-badge {
            position: absolute;
            top: -4px;
            right: -4px;
            min-width: 22px;
            height: 22px;
            padding: 0 6px;
            border-radius: 11px;
            background: #e30686;
            color: #fff;
            font-size: 12px;
            font-weight: 700;
            display: flex;
            align-items: center;
            justify-content: center;
            border: 2px solid #040f2a;
        }

        /* ===== Panel (open state) ===== */
        .jp-panel {
            position: fixed;
            bottom: 24px;
            right: 24px;
            width: 400px;
            height: min(640px, calc(100vh - 48px));
            background: #fff;
            border-radius: 18px;
            box-shadow: 0 25px 70px rgba(4,15,42,0.4);
            overflow: hidden;
            display: flex;
            flex-direction: column;
            transform-origin: bottom right;
            animation: jp-fadein-up 0.35s cubic-bezier(.68,-.01,.36,1);
        }

        @keyframes jp-fadein-up {
            from { opacity: 0; transform: translateY(18px) scale(0.96); }
            to   { opacity: 1; transform: translateY(0) scale(1); }
        }

        .jp-panel.jp-hidden { display: none; }

        /* ===== Header ===== */
        .jp-header {
            background: #040f2a;
            color: #fefefe;
            padding: 16px 48px 16px 18px;
            display: flex;
            align-items: center;
            gap: 12px;
            position: relative;
        }

        .jp-header::after {
            content: "";
            position: absolute;
            left: 0; right: 0; bottom: 0;
            height: 3px;
            background: linear-gradient(90deg,
                #e30686 0 25%, #feea03 25% 50%,
                #3e8fcd 50% 75%, #c4cf00 75% 100%);
        }

        .jp-logo {
            width: 42px;
            height: 42px;
            border-radius: 50%;
            background: #0a1836;
            overflow: hidden;
            flex-shrink: 0;
            border: 2px solid #1a2a4a;
        }
        .jp-logo img { width: 100%; height: 100%; object-fit: cover; }

        .jp-brand { flex: 1; min-width: 0; }

        .jp-brand-top {
            display: flex;
            align-items: center;
            gap: 7px;
            font-weight: 700;
            font-size: 15px;
        }

        .jp-ai-tag {
            background: #feea03;
            color: #040f2a;
            font-size: 9.5px;
            font-weight: 700;
            padding: 2px 5px;
            border-radius: 3px;
            letter-spacing: 0.6px;
            text-transform: uppercase;
        }

        .jp-tagline {
            margin-top: 3px;
            display: flex;
            gap: 6px;
            font-weight: 700;
            font-size: 9.5px;
            letter-spacing: 1px;
        }
        .jp-tagline .t1 { color: #e30686; }
        .jp-tagline .t2 { color: #feea03; }
        .jp-tagline .t3 { color: #3e8fcd; }
        .jp-tagline .t4 { color: #c4cf00; }

        .jp-close {
            position: absolute;
            top: 14px;
            right: 14px;
            width: 28px;
            height: 28px;
            border-radius: 50%;
            background: rgba(255,255,255,0.08);
            border: none;
            color: #fff;
            font-size: 20px;
            line-height: 1;
            cursor: pointer;
            display: flex;
            align-items: center;
            justify-content: center;
            transition: background 0.2s;
        }
        .jp-close:hover { background: rgba(255,255,255,0.18); }

        /* ===== Chat panel ===== */
        .jp-body {
            flex: 1;
            overflow: hidden;
            display: flex;
            flex-direction: column;
            position: relative;
        }

        .jp-view {
            flex: 1;
            overflow: hidden;
            display: flex;
            flex-direction: column;
            animation: jp-fadein 0.25s ease;
        }
        @keyframes jp-fadein {
            from { opacity: 0; }
            to   { opacity: 1; }
        }
        .jp-view.jp-hidden { display: none; }

        /* ===== Chat view ===== */
        .jp-messages {
            flex: 1;
            overflow-y: auto;
            padding: 18px 16px 80px;
            background: #f5f6fa;
            display: flex;
            flex-direction: column;
            gap: 8px;
            scroll-behavior: smooth;
        }

        .jp-messages::-webkit-scrollbar { width: 5px; }
        .jp-messages::-webkit-scrollbar-thumb { background: rgba(4,15,42,0.15); border-radius: 3px; }

        .jp-msg {
            max-width: 80%;
            padding: 10px 14px;
            border-radius: 14px;
            font-size: 14px;
            line-height: 1.45;
            word-wrap: break-word;
            white-space: pre-wrap;
            animation: jp-msg-in 0.3s ease-out;
        }
        @keyframes jp-msg-in {
            from { opacity: 0; transform: translateY(6px); }
            to   { opacity: 1; transform: translateY(0); }
        }

        .jp-msg.user {
            align-self: flex-end;
            background: #040f2a;
            color: #fefefe;
            border-bottom-right-radius: 4px;
        }
        .jp-msg.assistant {
            align-self: flex-start;
            background: #fff;
            color: #040f2a;
            border-bottom-left-radius: 4px;
            box-shadow: 0 1px 2px rgba(4,15,42,0.06);
        }
        .jp-msg.system {
            align-self: center;
            background: transparent;
            color: #6b7a99;
            font-size: 11.5px;
            font-style: italic;
            max-width: 90%;
            text-align: center;
            padding: 4px 10px;
        }

        /* ===== Error banner ===== */
        .jp-error-banner {
            background: #fff4f4;
            color: #040f2a;
            border-left: 4px solid #d33;
            padding: 10px 14px;
            margin: 8px 0;
            border-radius: 6px;
            font-size: 13px;
            line-height: 1.4;
            display: flex;
            align-items: flex-start;
            gap: 8px;
            animation: jp-fade-in 200ms ease;
        }
        .jp-error-banner.jp-hidden { display: none; }
        .jp-error-banner-icon {
            flex-shrink: 0;
            font-size: 16px;
            line-height: 1.2;
        }
        .jp-error-banner-close {
            background: transparent;
            border: 0;
            color: #6b7a99;
            cursor: pointer;
            font-size: 18px;
            line-height: 1;
            padding: 0 0 0 8px;
        }
        @keyframes jp-fade-in {
            from { opacity: 0; transform: translateY(-4px); }
            to { opacity: 1; transform: translateY(0); }
        }

        .jp-typing {
            align-self: flex-start;
            background: #fff;
            border-bottom-left-radius: 4px;
            padding: 13px 15px;
            display: flex;
            gap: 4px;
            box-shadow: 0 1px 2px rgba(4,15,42,0.06);
            border-radius: 14px;
        }
        .jp-typing span {
            width: 6px; height: 6px;
            border-radius: 50%;
            animation: jp-bounce 1.4s infinite ease-in-out;
        }
        .jp-typing span:nth-child(1) { background: #e30686; }
        .jp-typing span:nth-child(2) { background: #feea03; animation-delay: 0.2s; }
        .jp-typing span:nth-child(3) { background: #3e8fcd; animation-delay: 0.4s; }
        @keyframes jp-bounce {
            0%, 60%, 100% { transform: translateY(0); opacity: 0.5; }
            30% { transform: translateY(-5px); opacity: 1; }
        }

        /* ===== Quote card ===== */
        .jp-quote-card {
            align-self: flex-start;
            max-width: 85%;
            background: #fff;
            border-radius: 14px;
            overflow: visible;
            box-shadow: 0 2px 8px rgba(4,15,42,0.10);
            animation: jp-msg-in 0.4s ease-out;
            margin-bottom: 8px;
        }
        .jp-quote-card-header {
            background: #040f2a;
            color: #fefefe;
            padding: 10px 14px;
            font-size: 12px;
            font-weight: 600;
            letter-spacing: 0.5px;
            display: flex;
            align-items: center;
            gap: 6px;
        }
        .jp-quote-card-body {
            padding: 14px 14px 16px;
        }
        .jp-quote-card-product {
            font-weight: 600;
            font-size: 14px;
            color: #040f2a;
            margin-bottom: 2px;
        }
        .jp-quote-card-specs {
            font-size: 12px;
            color: #6b7a99;
            margin-bottom: 8px;
        }
        .jp-quote-card-total {
            font-size: 20px;
            font-weight: 700;
            color: #040f2a;
            margin-bottom: 10px;
        }
        .jp-quote-card-actions {
            display: flex;
            gap: 8px;
            padding-top: 4px;
        }
        .jp-card-btn {
            flex: 1;
            padding: 10px 12px;
            border-radius: 8px;
            border: none;
            font-family: inherit;
            font-size: 13px;
            font-weight: 600;
            cursor: pointer;
            transition: opacity 0.2s, transform 0.15s;
            display: flex;
            align-items: center;
            justify-content: center;
            gap: 5px;
            text-decoration: none;
        }
        .jp-card-btn:hover { opacity: 0.85; transform: translateY(-1px); }
        .jp-card-btn:active { transform: translateY(0); }
        .jp-card-btn.view {
            background: #040f2a;
            color: #fefefe;
        }
        .jp-card-btn.download {
            background: #f0f2f5;
            color: #040f2a;
        }
        .jp-quote-card .jp-rainbow-bar {
            height: 3px;
            background: linear-gradient(90deg, #e30686, #feea03, #3e8fcd, #00ff7f, #ff6347);
        }

        /* ===== Quote loading animation ===== */
        .jp-quote-loading {
            align-self: stretch;
            width: 100%;
            min-height: 160px;
            background: linear-gradient(135deg, #040f2a 0%, #0d1b3e 100%);
            border-radius: 14px;
            padding: 32px 24px 28px;
            animation: jp-msg-in 0.4s ease-out;
            overflow: hidden;
            position: relative;
            text-align: center;
            display: flex;
            flex-direction: column;
            align-items: center;
            justify-content: center;
        }
        .jp-quote-loading-icon {
            font-size: 36px;
            margin-bottom: 12px;
            animation: jp-printer-bob 1.2s ease-in-out infinite;
            display: block;
        }
        @keyframes jp-printer-bob {
            0%, 100% { transform: translateY(0) scale(1); }
            50% { transform: translateY(-6px) scale(1.05); }
        }
        .jp-quote-loading-text {
            color: #fefefe;
            font-size: 15px;
            font-weight: 600;
            margin-bottom: 6px;
            animation: jp-pulse-text 2s ease-in-out infinite;
        }
        .jp-quote-loading-sub {
            color: rgba(255,255,255,0.5);
            font-size: 11px;
            margin-bottom: 16px;
        }
        @keyframes jp-pulse-text {
            0%, 100% { opacity: 0.7; }
            50% { opacity: 1; }
        }
        .jp-quote-loading-bar {
            height: 4px;
            border-radius: 3px;
            background: rgba(255,255,255,0.1);
            overflow: hidden;
        }
        .jp-quote-loading-bar::after {
            content: '';
            display: block;
            height: 100%;
            width: 30%;
            border-radius: 3px;
            background: linear-gradient(90deg, #e30686, #feea03, #3e8fcd, #00ff7f, #ff6347);
            animation: jp-rainbow-slide 1.8s ease-in-out infinite;
        }
        @keyframes jp-rainbow-slide {
            0% { transform: translateX(-100%); }
            100% { transform: translateX(450%); }
        }

        /* ===== Input area ===== */
        .jp-input-area {
            padding: 12px;
            background: #fff;
            border-top: 1px solid #e4e7ee;
            display: flex;
            gap: 8px;
            align-items: center;
        }

        .jp-input {
            flex: 1;
            padding: 11px 16px;
            border: 1.5px solid #e4e7ee;
            border-radius: 22px;
            outline: none;
            font-family: inherit;
            font-size: 14px;
            color: #040f2a;
            background: #fff;
            transition: border-color 0.2s, box-shadow 0.2s;
        }
        .jp-input:focus {
            border-color: #040f2a;
            box-shadow: 0 0 0 3px rgba(4,15,42,0.08);
        }
        .jp-input:disabled { background: #f8fafc; cursor: not-allowed; }

        .jp-send {
            width: 42px;
            height: 42px;
            border: none;
            background: #040f2a;
            color: #fefefe;
            border-radius: 50%;
            cursor: pointer;
            display: flex;
            align-items: center;
            justify-content: center;
            flex-shrink: 0;
            transition: transform 0.2s;
            position: relative;
            overflow: hidden;
        }
        .jp-send::before {
            content: "";
            position: absolute;
            inset: 0;
            background: linear-gradient(135deg, #e30686, #feea03, #3e8fcd, #c4cf00);
            opacity: 0;
            transition: opacity 0.25s;
        }
        .jp-send:hover::before { opacity: 1; }
        .jp-send:hover { transform: scale(1.08); }
        .jp-send svg { width: 18px; height: 18px; position: relative; z-index: 1; }
        .jp-send:disabled { background: #c4cad6; cursor: not-allowed; transform: none; }
        .jp-send:disabled::before { opacity: 0; }

        /* ===== Inline image (size guide etc) ===== */
        .jp-msg-img {
            align-self: flex-start;
            max-width: 85%;
            border-radius: 12px;
            overflow: hidden;
            box-shadow: 0 2px 8px rgba(4,15,42,0.10);
            animation: jp-msg-in 0.3s ease-out;
            cursor: pointer;
        }
        .jp-msg-img img {
            width: 100%;
            display: block;
        }

        /* (Quick Quote form CSS removed) */
        .jp-REMOVED {
            font-size: 16px;
            font-weight: 700;
            color: #040f2a;
            margin-bottom: 4px;
        }
        .jp-form-view p.jp-sub {
            color: #6b7a99;
            font-size: 12.5px;
            margin-bottom: 16px;
        }

        .jp-field {
            margin-bottom: 12px;
            animation: jp-field-in 0.3s ease both;
        }
        @keyframes jp-field-in {
            from { opacity: 0; transform: translateX(-8px); }
            to   { opacity: 1; transform: translateX(0); }
        }
        .jp-field:nth-child(1) { animation-delay: 0.05s; }
        .jp-field:nth-child(2) { animation-delay: 0.10s; }
        .jp-field:nth-child(3) { animation-delay: 0.15s; }
        .jp-field:nth-child(4) { animation-delay: 0.20s; }
        .jp-field:nth-child(5) { animation-delay: 0.25s; }
        .jp-field:nth-child(6) { animation-delay: 0.30s; }

        .jp-field label {
            display: block;
            font-size: 11px;
            font-weight: 700;
            letter-spacing: 0.8px;
            text-transform: uppercase;
            color: #040f2a;
            margin-bottom: 6px;
        }

        .jp-field select,
        .jp-field input[type="number"],
        .jp-field input[type="text"] {
            width: 100%;
            padding: 11px 14px;
            border: 1.5px solid #e4e7ee;
            border-radius: 10px;
            background: #fff;
            font-family: inherit;
            font-size: 14px;
            color: #040f2a;
            transition: border-color 0.2s, box-shadow 0.2s;
            appearance: none;
            -webkit-appearance: none;
        }

        .jp-field select {
            background-image: url("data:image/svg+xml;utf8,<svg xmlns='http://www.w3.org/2000/svg' width='12' height='12' viewBox='0 0 12 12'><path d='M2 4l4 4 4-4' stroke='%23040f2a' stroke-width='1.5' fill='none' stroke-linecap='round' stroke-linejoin='round'/></svg>");
            background-repeat: no-repeat;
            background-position: right 14px center;
            padding-right: 36px;
        }

        .jp-field select:focus,
        .jp-field input:focus {
            outline: none;
            border-color: #040f2a;
            box-shadow: 0 0 0 3px rgba(4,15,42,0.08);
        }

        .jp-field .jp-checkbox {
            display: flex;
            align-items: center;
            gap: 8px;
            font-size: 13px;
            color: #040f2a;
            cursor: pointer;
            padding: 10px 12px;
            background: #fff;
            border: 1.5px solid #e4e7ee;
            border-radius: 10px;
        }
        .jp-field .jp-checkbox input { accent-color: #e30686; }

        .jp-quote-btn {
            width: 100%;
            padding: 13px;
            background: #040f2a;
            color: #fff;
            border: none;
            border-radius: 10px;
            font-family: inherit;
            font-weight: 700;
            font-size: 14px;
            letter-spacing: 0.5px;
            cursor: pointer;
            margin-top: 6px;
            transition: transform 0.2s, background 0.25s;
            position: relative;
            overflow: hidden;
        }
        .jp-quote-btn::before {
            content: "";
            position: absolute;
            inset: 0;
            background: linear-gradient(135deg, #e30686, #feea03, #3e8fcd, #c4cf00);
            opacity: 0;
            transition: opacity 0.3s;
        }
        .jp-quote-btn span { position: relative; z-index: 1; }
        .jp-quote-btn:hover::before { opacity: 1; }
        .jp-quote-btn:hover { transform: translateY(-1px); }
        .jp-quote-btn:disabled { opacity: 0.5; cursor: not-allowed; transform: none; }
        .jp-quote-btn:disabled::before { opacity: 0; }

        /* Result card */
        .jp-result {
            margin-top: 14px;
            padding: 16px;
            background: #fff;
            border-radius: 12px;
            border-left: 4px solid #feea03;
            box-shadow: 0 2px 6px rgba(4,15,42,0.06);
            animation: jp-fadein-up 0.4s cubic-bezier(.68,-.01,.36,1);
        }
        .jp-result.jp-escalation { border-left-color: #e30686; }
        .jp-result h4 {
            font-size: 13px;
            color: #6b7a99;
            font-weight: 600;
            margin-bottom: 6px;
        }
        .jp-result .jp-price {
            font-size: 26px;
            font-weight: 700;
            color: #040f2a;
            margin-bottom: 2px;
        }
        .jp-result .jp-price-line {
            color: #6b7a99;
            font-size: 12px;
            margin-bottom: 10px;
        }
        .jp-result .jp-chip {
            display: inline-block;
            font-size: 11px;
            padding: 3px 8px;
            border-radius: 10px;
            background: rgba(254,234,3,0.25);
            color: #7a5d00;
            margin-right: 4px;
            margin-bottom: 4px;
            font-weight: 600;
        }
        .jp-result .jp-note {
            font-size: 12px;
            color: #6b7a99;
            margin-top: 8px;
            border-top: 1px dashed #e4e7ee;
            padding-top: 8px;
        }

        /* ===== Mobile ===== */
        @media (max-width: 480px) {
            .jp-panel {
                right: 0;
                bottom: 0;
                width: 100%;
                height: 100vh;
                max-height: 100vh;
                border-radius: 0;
            }
            .jp-bubble {
                right: 16px;
                bottom: 16px;
            }
        }

        /* ===== Phase F — Customer info form ===== */
        .jp-form-card {
            align-self: stretch;
            width: 100%;
            background: #fff;
            border: 1px solid #e1e6f0;
            border-radius: 14px;
            padding: 16px 16px 18px;
            margin: 4px 0 8px;
            animation: jp-msg-in 0.35s ease-out;
            box-shadow: 0 2px 8px rgba(4,15,42,0.08);
        }
        .jp-form-card.submitted {
            text-align: center;
            padding: 18px 16px;
            background: #f0fdf4;
            border-color: #bbf7d0;
        }
        .jp-form-title {
            font-weight: 700;
            font-size: 15px;
            color: #040f2a;
            margin-bottom: 4px;
        }
        .jp-form-sub {
            font-size: 12px;
            color: #6b7a99;
            margin-bottom: 14px;
        }
        .jp-form-card form {
            display: flex;
            flex-direction: column;
            gap: 10px;
        }
        .jp-form-label {
            display: flex;
            flex-direction: column;
            gap: 4px;
            font-size: 12px;
            font-weight: 600;
            color: #2f3a55;
        }
        .jp-form-label input,
        .jp-form-label select {
            font-family: inherit;
            font-size: 13px;
            padding: 8px 10px;
            border: 1px solid #d6dde9;
            border-radius: 8px;
            background: #fff;
            color: #040f2a;
            font-weight: 400;
            outline: none;
            transition: border 0.15s, box-shadow 0.15s;
        }
        .jp-form-label input:focus,
        .jp-form-label select:focus {
            border-color: #3e8fcd;
            box-shadow: 0 0 0 3px rgba(62,143,205,0.15);
        }
        .jp-form-label input:invalid:not(:placeholder-shown) {
            border-color: #dc2626;
        }
        .jp-form-collect-block {
            background: #f6f8fc;
            border-radius: 8px;
            padding: 10px 12px;
            font-size: 12px;
            color: #2f3a55;
            line-height: 1.5;
        }
        .jp-form-err {
            background: #fef2f2;
            border: 1px solid #fecaca;
            color: #991b1b;
            border-radius: 8px;
            padding: 8px 10px;
            font-size: 12px;
            margin-top: 4px;
        }
        .jp-form-submit {
            margin-top: 6px;
            padding: 11px 14px;
            background: #040f2a;
            color: #fefefe;
            border: none;
            border-radius: 8px;
            font-family: inherit;
            font-size: 14px;
            font-weight: 600;
            cursor: pointer;
            transition: opacity 0.15s, transform 0.1s;
        }
        .jp-form-submit:hover { opacity: 0.9; }
        .jp-form-submit:active { transform: translateY(1px); }
        .jp-form-submit:disabled { opacity: 0.6; cursor: not-allowed; }

        /* ===== Phase F — Artwork upload card ===== */
        .jp-upload-card {
            align-self: flex-start;
            max-width: 92%;
            background: #fff;
            border: 1px solid #e1e6f0;
            border-radius: 12px;
            padding: 12px;
            margin: 4px 0 6px;
            animation: jp-msg-in 0.35s ease-out;
        }
        .jp-upload-row {
            display: flex;
            align-items: center;
            gap: 10px;
        }
        .jp-upload-icon { font-size: 22px; }
        .jp-upload-body { flex: 1; min-width: 0; }
        .jp-upload-title { font-weight: 600; font-size: 13px; color: #040f2a; }
        .jp-upload-sub { font-size: 11px; color: #6b7a99; margin-top: 1px; }
        .jp-upload-btn {
            background: #040f2a;
            color: #fefefe;
            border: none;
            border-radius: 8px;
            padding: 8px 12px;
            font-family: inherit;
            font-size: 12px;
            font-weight: 600;
            cursor: pointer;
        }
        .jp-upload-btn:hover { opacity: 0.9; }
        .jp-upload-btn:disabled { opacity: 0.6; cursor: not-allowed; }
        .jp-upload-status {
            margin-top: 8px;
            font-size: 12px;
            padding: 6px 8px;
            border-radius: 6px;
            background: #f6f8fc;
            color: #2f3a55;
        }
        .jp-upload-status.ok { background: #f0fdf4; color: #166534; }
        .jp-upload-status.err { background: #fef2f2; color: #991b1b; }

        /* ===== Phase G v26 — Artwork-choice buttons ===== */
        .jp-choice-card {
            align-self: flex-start;
            max-width: 92%;
            display: flex;
            flex-direction: column;
            gap: 8px;
            margin: 4px 0 6px;
            animation: jp-msg-in 0.35s ease-out;
        }
        .jp-choice-btn {
            background: #fff;
            border: 1.5px solid #e1e6f0;
            border-radius: 12px;
            padding: 12px 16px;
            text-align: left;
            font-family: inherit;
            cursor: pointer;
            transition: all 0.15s ease;
        }
        .jp-choice-btn:hover:not(:disabled) {
            border-color: #040f2a;
            background: #f6f8fc;
        }
        .jp-choice-btn:disabled { cursor: default; }
        .jp-choice-btn-selected {
            background: #040f2a !important;
            border-color: #040f2a !important;
        }
        .jp-choice-btn-selected .jp-choice-title,
        .jp-choice-btn-selected .jp-choice-sub { color: #fefefe; }
        .jp-choice-btn-faded { opacity: 0.4; }
        .jp-choice-title {
            font-weight: 600;
            font-size: 13px;
            color: #040f2a;
        }
        .jp-choice-sub {
            font-size: 11px;
            color: #6b7a99;
            margin-top: 2px;
        }

        /* Phase G — multi-file artwork list */
        .jp-upload-list {
            margin-top: 10px;
            display: flex;
            flex-direction: column;
            gap: 6px;
        }
        .jp-upload-file {
            display: flex;
            align-items: center;
            gap: 8px;
            padding: 8px 10px;
            background: #f0fdf4;
            border: 1px solid #bbf7d0;
            border-radius: 8px;
            font-size: 12px;
        }
        .jp-upload-file-icon { font-size: 14px; flex-shrink: 0; }
        .jp-upload-file-name {
            flex: 1;
            min-width: 0;
            font-weight: 600;
            color: #166534;
            overflow: hidden;
            text-overflow: ellipsis;
            white-space: nowrap;
        }
        .jp-upload-file-size {
            color: #4d7c5a;
            font-size: 11px;
            flex-shrink: 0;
        }
        .jp-upload-file-remove {
            background: transparent;
            border: none;
            color: #166534;
            cursor: pointer;
            padding: 2px 6px;
            border-radius: 4px;
            font-size: 13px;
            font-weight: 600;
            line-height: 1;
            flex-shrink: 0;
        }
        .jp-upload-file-remove:hover { background: #dcfce7; color: #991b1b; }
    `;

    // ======================================================================
    // DOM MARKUP
    // ======================================================================
    const HTML = `
        <button class="jp-bubble" id="jpBubble" aria-label="Open chat">
            <img src="https://just-print.ie/wp-content/themes/just-print/assets/img/tiger_760.png" alt="Just-Print">
            <span class="jp-bubble-badge" id="jpBadge" style="display:none;">1</span>
        </button>

        <div class="jp-panel jp-hidden" id="jpPanel" role="dialog" aria-label="Just-Print quote assistant">
            <div class="jp-header">
                <div class="jp-logo">
                    <img src="https://just-print.ie/wp-content/themes/just-print/assets/img/tiger_760.png" alt="logo">
                </div>
                <div class="jp-brand">
                    <div class="jp-brand-top">
                        <span>Just-Print.ie</span>
                        <span class="jp-ai-tag">Craig</span>
                    </div>
                    <div class="jp-tagline">
                        <span class="t1">PRINT</span>
                        <span class="t2">DESIGN</span>
                        <span class="t3">SIGNAGE</span>
                        <span class="t4">&amp; MORE</span>
                    </div>
                </div>
                <button class="jp-close" id="jpClose" aria-label="Close">×</button>
            </div>

            <div class="jp-body">
                <div class="jp-view" id="jpChatView">
                    <div class="jp-error-banner jp-hidden" id="jpErrorBanner" role="alert" aria-live="polite">
                        <span class="jp-error-banner-icon">⚠</span>
                        <span id="jpErrorBannerText">Couldn't reach Just Print's quoting agent. Try again in a moment, or email info@just-print.ie.</span>
                        <button class="jp-error-banner-close" id="jpErrorBannerClose" aria-label="Dismiss">×</button>
                    </div>
                    <div class="jp-messages" id="jpMessages"></div>
                    <div class="jp-input-area">
                        <input type="text" class="jp-input" id="jpInput" placeholder="What are you looking to print?" autocomplete="off">
                        <button class="jp-send" id="jpSendBtn" aria-label="Send">
                            <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round">
                                <line x1="22" y1="2" x2="11" y2="13"></line>
                                <polygon points="22 2 15 22 11 13 2 9 22 2"></polygon>
                            </svg>
                        </button>
                    </div>
                </div>
            </div>
        </div>
    `;

    // ======================================================================
    // MOUNT
    // ======================================================================

    function mount() {
        // Avoid double-mount
        if (document.getElementById('jpBubble')) return;

        // Inject base styles
        const style = document.createElement('style');
        style.textContent = STYLES;
        document.head.appendChild(style);

        // Inject per-tenant CSS variables + build the rainbow stripe from the
        // tenant's accents array and chosen stripe mode. The same background
        // value is used for the header underline, the quote-card top bar and
        // the loading progress bar so they stay visually consistent.
        const stripeBg = buildStripeBackground(
            WIDGET_CONFIG.accents,
            WIDGET_CONFIG.stripe_mode,
            WIDGET_CONFIG.primary_color,
        );
        // Typing dots cycle through the first few accents (fall back to primary).
        const accentList = (WIDGET_CONFIG.accents && WIDGET_CONFIG.accents.length)
            ? WIDGET_CONFIG.accents
            : [WIDGET_CONFIG.primary_color];
        const dot = function (i) { return accentList[i % accentList.length]; };

        const themeStyle = document.createElement('style');
        themeStyle.id = 'jp-widget-theme';
        themeStyle.textContent = `
            .jp-widget {
                --jp-primary: ${WIDGET_CONFIG.primary_color};
                --jp-stripe: ${stripeBg};
            }
            .jp-bubble,
            .jp-header,
            .jp-msg.user,
            .jp-send,
            .jp-card-btn.view,
            .jp-quote-card-header,
            .jp-pdf-modal-header { background: ${WIDGET_CONFIG.primary_color} !important; }
            .jp-tab.jp-active::after { background: ${dot(1)}; }
            .jp-typing span:nth-child(1) { background: ${dot(0)}; }
            .jp-typing span:nth-child(2) { background: ${dot(1)}; }
            .jp-typing span:nth-child(3) { background: ${dot(2)}; }
            .jp-header::after { background: ${stripeBg}; }
            .jp-quote-card .jp-rainbow-bar { background: ${stripeBg}; }
            .jp-quote-loading-bar::after { background: ${stripeBg}; }
        `;
        document.head.appendChild(themeStyle);

        // Inject the configured display font (Google Fonts)
        const fontName = (WIDGET_CONFIG.font || 'Poppins').replace(/\s+/g, '+');
        if (!document.querySelector(`link[href*="${fontName}"]`)) {
            const font = document.createElement('link');
            font.rel = 'stylesheet';
            font.href = `https://fonts.googleapis.com/css2?family=${fontName}:wght@400;500;600;700&family=Roboto:wght@400;500;700&display=swap`;
            document.head.appendChild(font);
        }

        const root = document.createElement('div');
        root.className = 'jp-widget';
        root.innerHTML = HTML;
        document.body.appendChild(root);

        // Replace the hardcoded tiger logo with the tenant's logo if set
        if (WIDGET_CONFIG.logo_url) {
            root.querySelectorAll('img').forEach((img) => {
                if (img.src.includes('tiger_760.png')) {
                    img.src = WIDGET_CONFIG.logo_url;
                }
            });
        }

        attachBehavior();
    }

    // ======================================================================
    // BEHAVIOR
    // ======================================================================

    let conversationId = null;
    const sessionId = 'web-' + Math.random().toString(36).slice(2, 11);
    let chatBooted = false;

    function attachBehavior() {
        const $ = (id) => document.getElementById(id);

        const bubble = $('jpBubble');
        const panel = $('jpPanel');
        const closeBtn = $('jpClose');
        const chatView = $('jpChatView');

        // --- Open / close ---
        function openPanel() {
            bubble.classList.add('jp-hidden');
            panel.classList.remove('jp-hidden');
            if (!chatBooted) {
                chatBooted = true;
                bootChat();
            }
        }
        function closePanel() {
            panel.classList.add('jp-hidden');
            bubble.classList.remove('jp-hidden');
        }

        bubble.addEventListener('click', openPanel);
        closeBtn.addEventListener('click', closePanel);

        // --- Chat ---
        const messagesEl = $('jpMessages');
        const input = $('jpInput');
        const sendBtn = $('jpSendBtn');
        const errorBanner = $('jpErrorBanner');
        const errorBannerText = $('jpErrorBannerText');
        const errorBannerClose = $('jpErrorBannerClose');

        // Error banner state. Auto-hides after 8s, hides on next successful
        // chat round-trip, or on click of the X. Shows on:
        //   - any catch (network down, DNS, etc.)
        //   - any res.status >= 500
        //   - any data.error field returned from the server
        let errorBannerTimer = null;
        function showErrorBanner(msg) {
            errorBannerText.textContent =
                msg || "Couldn't reach Just Print's quoting agent. Try again in a moment, or email info@just-print.ie.";
            errorBanner.classList.remove('jp-hidden');
            if (errorBannerTimer) clearTimeout(errorBannerTimer);
            errorBannerTimer = setTimeout(hideErrorBanner, 8000);
        }
        function hideErrorBanner() {
            errorBanner.classList.add('jp-hidden');
            if (errorBannerTimer) {
                clearTimeout(errorBannerTimer);
                errorBannerTimer = null;
            }
        }
        errorBannerClose.addEventListener('click', hideErrorBanner);

        let lastQuoteId = null;
        let lastQuoteData = null;  // store full tool_calls data from pricing turn

        function addMsg(text, role) {
            // Check for image markers before rendering
            if (text && text.includes('[SIZE_GUIDE]')) {
                var cleanText = text.replace(/\[SIZE_GUIDE\]/g, '').trim();
                if (cleanText) {
                    var msgEl = document.createElement('div');
                    msgEl.className = 'jp-msg ' + role;
                    msgEl.textContent = cleanText;
                    messagesEl.appendChild(msgEl);
                }
                addImage(API_BASE + '/static/images/size-guide.png', 'Page size guide');
                return;
            }
            var el = document.createElement('div');
            el.className = 'jp-msg ' + role;
            el.textContent = text;
            messagesEl.appendChild(el);
            messagesEl.scrollTop = messagesEl.scrollHeight;
        }

        function addImage(src, alt) {
            var wrapper = document.createElement('div');
            wrapper.className = 'jp-msg-img';
            var img = document.createElement('img');
            img.src = src;
            img.alt = alt || '';
            img.addEventListener('click', function() {
                window.open(src, '_blank');
            });
            wrapper.appendChild(img);
            messagesEl.appendChild(wrapper);
            messagesEl.scrollTop = messagesEl.scrollHeight;
        }

        function addHtml(html) {
            const wrapper = document.createElement('div');
            wrapper.innerHTML = html;
            while (wrapper.firstChild) {
                messagesEl.appendChild(wrapper.firstChild);
            }
            messagesEl.scrollTop = messagesEl.scrollHeight;
        }

        function showQuoteLoading() {
            const el = document.createElement('div');
            el.className = 'jp-quote-loading';
            el.id = 'jpQuoteLoading';
            el.innerHTML = `
                <div class="jp-quote-loading-icon">\uD83D\uDDA8\uFE0F</div>
                <div class="jp-quote-loading-text">Putting your quote together...</div>
                <div class="jp-quote-loading-sub">This\u2019ll only take a sec</div>
                <div class="jp-quote-loading-bar"></div>
            `;
            messagesEl.appendChild(el);
            messagesEl.scrollTop = messagesEl.scrollHeight;
        }

        function removeQuoteLoading() {
            const el = document.getElementById('jpQuoteLoading');
            if (el) el.remove();
        }

        function showQuoteCard(quoteId) {
            // Pure DOM — no fetch, no async, cannot fail
            var pdfUrl = API_BASE + '/quotes/' + quoteId + '/pdf';
            var refId = 'JP-' + String(quoteId).padStart(4, '0');

            var card = document.createElement('div');
            card.className = 'jp-quote-card';
            card.innerHTML = '<div class="jp-rainbow-bar"></div>'
                + '<div class="jp-quote-card-header">\uD83D\uDCCB Your quote is ready!</div>'
                + '<div class="jp-quote-card-body">'
                + '<div class="jp-quote-card-product">Ref: ' + refId + '</div>'
                + '<div class="jp-quote-card-specs">View or download your branded quote below</div>'
                + '<div class="jp-quote-card-actions">'
                + '<button class="jp-card-btn view" id="jpViewBtn' + quoteId + '">\uD83D\uDCC4 View Quote</button>'
                + '<a href="' + pdfUrl + '" download class="jp-card-btn download">\u2B07\uFE0F Download</a>'
                + '</div></div>';

            messagesEl.appendChild(card);

            // Force scroll to absolute bottom so buttons are visible
            setTimeout(function() {
                messagesEl.scrollTop = messagesEl.scrollHeight + 500;
            }, 50);
            setTimeout(function() {
                messagesEl.scrollTop = messagesEl.scrollHeight + 500;
            }, 200);

            // Attach click handler directly (no onclick string)
            document.getElementById('jpViewBtn' + quoteId).addEventListener('click', function() {
                window._jpOpenPdf(pdfUrl, quoteId);
            });
        }

        // Open PDF in a new tab (more reliable than iframe modal)
        window._jpOpenPdf = function(url, quoteId) {
            window.open(url, '_blank');
        };

        // ─────────────────────────────────────────────────────────────
        // Phase F — interactive customer-info form (replaces free-text Q&A)
        // ─────────────────────────────────────────────────────────────

        function showCustomerForm() {
            // Lock the chat input visibly so the customer knows the
            // form is the active surface, not the text box.
            input.placeholder = "Fill in the form above to continue …";
            input.disabled = true;
            sendBtn.disabled = true;

            const formEl = document.createElement('div');
            formEl.className = 'jp-form-card';
            formEl.id = 'jpForm';
            formEl.innerHTML = (
                '<div class="jp-form-title">A few more details</div>'
                + '<div class="jp-form-sub">All fields marked * are required.</div>'
                + '<form id="jpFormEl" autocomplete="on">'
                + '  <label class="jp-form-label">Your name *<input type="text" name="name" required minlength="2" maxlength="200"></label>'
                + '  <label class="jp-form-label">Email *<input type="email" name="email" required></label>'
                + '  <label class="jp-form-label">Phone (optional)<input type="tel" name="phone" placeholder="+353 ..."></label>'
                + '  <label class="jp-form-label">Are you ordering as *'
                + '    <select name="is_company" required>'
                + '      <option value="false" selected>Individual</option>'
                + '      <option value="true">Company</option>'
                + '    </select>'
                + '  </label>'
                + '  <label class="jp-form-label">Have you ordered with us before?'
                + '    <select name="is_returning_customer" id="jpReturning">'
                + '      <option value="false" selected>No</option>'
                + '      <option value="true">Yes</option>'
                + '    </select>'
                + '  </label>'
                + '  <label class="jp-form-label" id="jpPastEmailWrap" style="display:none;">Email you used last time'
                + '    <input type="email" name="past_customer_email">'
                + '  </label>'
                + '  <label class="jp-form-label">How would you like to receive it? *'
                + '    <select name="delivery_method" id="jpDeliveryMethod" required>'
                + '      <option value="" disabled selected>Choose one…</option>'
                + '      <option value="delivery">' + (() => {
                    // Phase G - dynamic shipping label based on quote total
                    const goods = (lastQuoteData && typeof lastQuoteData.quote_total_inc_vat === 'number')
                        ? lastQuoteData.quote_total_inc_vat : null;
                    if (goods !== null && goods >= 100) {
                        return 'Just Print Delivery (free - over €100 order)';
                    }
                    if (goods !== null && goods > 0) {
                        const away = (100 - goods).toFixed(2);
                        return 'Just Print Delivery (+€15) - free over €100, you are €' + away + ' away';
                    }
                    return 'Just Print Delivery (+€15, free over €100)';
                })() + '</option>'
                + '      <option value="collect">Collection from our shop</option>'
                + '    </select>'
                + '  </label>'
                + '  <div id="jpDeliveryAddrWrap" style="display:none;">'
                + '    <label class="jp-form-label">Address line 1 *<input type="text" name="address1" maxlength="200"></label>'
                + '    <label class="jp-form-label">Address line 2 (optional)<input type="text" name="address2" maxlength="200"></label>'
                + '    <label class="jp-form-label">Town / City *<input type="text" name="address4" maxlength="200"></label>'
                + '    <label class="jp-form-label">Eircode *<input type="text" name="postcode" placeholder="D02 X1Y2" maxlength="8"></label>'
                + '  </div>'
                + '  <div id="jpCollectInfo" style="display:none;" class="jp-form-collect-block">'
                + '    📍 <strong>Pickup at:</strong><br>'
                + '    Ballymount Cross Business Park, 7,<br>Ballymount, Dublin, D24 E5NH'
                + '  </div>'
                + '  <div id="jpFormErr" class="jp-form-err" style="display:none;"></div>'
                + '  <button type="submit" class="jp-form-submit" id="jpFormSubmit">Send my details</button>'
                + '</form>'
            );
            messagesEl.appendChild(formEl);
            messagesEl.scrollTop = messagesEl.scrollHeight;

            const form = document.getElementById('jpFormEl');
            const returning = document.getElementById('jpReturning');
            const pastWrap = document.getElementById('jpPastEmailWrap');
            const dm = document.getElementById('jpDeliveryMethod');
            const addrWrap = document.getElementById('jpDeliveryAddrWrap');
            const collectInfo = document.getElementById('jpCollectInfo');
            const errEl = document.getElementById('jpFormErr');
            const submitBtn = document.getElementById('jpFormSubmit');

            returning.addEventListener('change', () => {
                pastWrap.style.display = returning.value === 'true' ? 'block' : 'none';
                if (returning.value !== 'true') {
                    pastWrap.querySelector('input').value = '';
                }
            });
            dm.addEventListener('change', () => {
                addrWrap.style.display = dm.value === 'delivery' ? 'block' : 'none';
                collectInfo.style.display = dm.value === 'collect' ? 'block' : 'none';
                // Address inputs only required when delivery
                addrWrap.querySelectorAll('input').forEach((el) => {
                    if (el.name === 'address2') return;  // optional
                    el.required = (dm.value === 'delivery');
                });
            });

            form.addEventListener('submit', async (e) => {
                e.preventDefault();
                errEl.style.display = 'none';
                errEl.textContent = '';

                const fd = new FormData(form);
                const method = fd.get('delivery_method');
                const body = {
                    external_id: sessionId,
                    name: (fd.get('name') || '').trim(),
                    email: (fd.get('email') || '').trim(),
                    phone: (fd.get('phone') || '').trim() || null,
                    is_company: fd.get('is_company') === 'true',
                    is_returning_customer: fd.get('is_returning_customer') === 'true',
                    past_customer_email: (fd.get('past_customer_email') || '').trim() || null,
                    delivery_method: method,
                };
                if (method === 'delivery') {
                    body.delivery_address = {
                        address1: (fd.get('address1') || '').trim(),
                        address2: (fd.get('address2') || '').trim(),
                        address3: '',
                        address4: (fd.get('address4') || '').trim(),
                        postcode: (fd.get('postcode') || '').trim(),
                    };
                    if (!/^[A-Za-z]\d{2}\s?[A-Za-z0-9]{4}$/.test(body.delivery_address.postcode)) {
                        errEl.textContent = 'Please enter a valid Irish eircode (e.g. D02 X1Y2).';
                        errEl.style.display = 'block';
                        return;
                    }
                }

                submitBtn.disabled = true;
                submitBtn.textContent = 'Saving…';

                try {
                    const resp = await fetch(
                        API_BASE + '/widget/conversations/' + conversationId + '/customer-info',
                        {
                            method: 'POST',
                            headers: { 'Content-Type': 'application/json' },
                            body: JSON.stringify(body),
                        },
                    );
                    if (!resp.ok) {
                        let msg = 'Could not save details (HTTP ' + resp.status + ').';
                        try {
                            const j = await resp.json();
                            if (j.detail) msg = typeof j.detail === 'string' ? j.detail : JSON.stringify(j.detail);
                        } catch (_) { /* swallow */ }
                        errEl.textContent = msg;
                        errEl.style.display = 'block';
                        submitBtn.disabled = false;
                        submitBtn.textContent = 'Send my details';
                        return;
                    }
                    const result = await resp.json();
                    // Form succeeded — collapse it into a confirmation summary
                    formEl.classList.add('submitted');
                    formEl.innerHTML = (
                        '<div class="jp-form-title">✓ Got everything</div>'
                        + '<div class="jp-form-sub">' + escapeHtml(body.name) + ' · '
                        + escapeHtml(body.email) + '</div>'
                    );
                    input.placeholder = 'Type a message…';

                    // Phase F refined — server already wrote the canned
                    // assistant reply into the conversation transcript +
                    // returned the text + quote_id. Render it directly,
                    // no /chat round-trip needed.
                    if (result.assistant_reply) {
                        addMsg(result.assistant_reply, 'assistant');
                    }
                    const finalQuoteId = result.quote_id || lastQuoteId;
                    if (finalQuoteId) {
                        showQuoteLoading();
                        await new Promise(function (resolve) { setTimeout(resolve, 1400); });
                        removeQuoteLoading();
                        showQuoteCard(finalQuoteId);
                        lastQuoteId = finalQuoteId;
                    }
                    // Re-enable the input so the customer can ask follow-ups.
                    input.disabled = false;
                    sendBtn.disabled = false;
                    input.focus();
                } catch (e) {
                    errEl.textContent = 'Network error: ' + e.message;
                    errEl.style.display = 'block';
                    submitBtn.disabled = false;
                    submitBtn.textContent = 'Send my details';
                }
            });
        }

        function escapeHtml(s) {
            return String(s || '').replace(/[&<>"']/g, (c) => ({
                '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;',
            })[c]);
        }

        // ─────────────────────────────────────────────────────────────
        // Phase F — artwork upload button
        // ─────────────────────────────────────────────────────────────

        // Phase G — multi-file artwork upload state
        const ARTWORK_MAX_FILES = 10;
        let uploadedFiles = [];  // [{ url, filename, size, content_type, uploaded_at }]

        async function _uploadOneArtwork(file, wrap) {
            const statusEl = document.getElementById('jpUploadStatus');
            if (file.size > 100 * 1024 * 1024) {
                if (statusEl) {
                    statusEl.textContent = 'File "' + file.name + '" too big - max 100 MB.';
                    statusEl.style.display = 'block';
                    statusEl.classList.add('err');
                    statusEl.classList.remove('ok');
                }
                return;
            }
            if (statusEl) {
                statusEl.classList.remove('err');
                statusEl.classList.add('ok');
                statusEl.style.display = 'block';
                statusEl.textContent = 'Uploading ' + file.name + ' ...';
            }
            const fd = new FormData();
            fd.append('file', file);
            fd.append('external_id', sessionId);
            try {
                const resp = await fetch(
                    API_BASE + '/widget/conversations/' + conversationId + '/upload-artwork',
                    { method: 'POST', body: fd },
                );
                if (!resp.ok) {
                    let msg = 'Upload failed (HTTP ' + resp.status + ').';
                    try {
                        const j = await resp.json();
                        if (j.detail) msg = typeof j.detail === 'string' ? j.detail : JSON.stringify(j.detail);
                    } catch (_) { /* swallow */ }
                    if (statusEl) {
                        statusEl.textContent = msg;
                        statusEl.classList.remove('ok');
                        statusEl.classList.add('err');
                    }
                    return;
                }
                const result = await resp.json();
                uploadedFiles = result.files || [];
                _renderArtworkCard(wrap);

                // Phase G refined — uploading IS the answer to "do you
                // have your own artwork?". On the FIRST successful upload
                // (server tells us via first_upload=true), fire a
                // synthetic chat turn so Craig acknowledges + auto-advances
                // (re-prices with needs_artwork=false, asks "want full
                // quote?", etc). This is the user's explicit ask:
                // "subir los docs es el equivalente de que el usuario
                // hubiese respondido en el chat".
                if (result.first_upload && !_uploadAdvanceTriggered) {
                    _uploadAdvanceTriggered = true;
                    if (statusEl) {
                        statusEl.classList.remove('err');
                        statusEl.classList.add('ok');
                        statusEl.style.display = 'block';
                        statusEl.textContent = 'Got it — your artwork is in. One sec…';
                    }
                    // Synthetic user turn — no user bubble. Phrased like a
                    // natural user message so the server-side artwork
                    // sniffer (_ARTWORK_HAVE_AFFIRMATIVE) catches it and
                    // stamps customer_has_own_artwork=True. Craig's next
                    // turn will then re-price w/ needs_artwork=False (or
                    // skip straight to "want full quote?") instead of
                    // re-asking the artwork question.
                    sendMessage(
                        "I've uploaded my artwork."
                    );
                }
            } catch (e) {
                if (statusEl) {
                    statusEl.textContent = 'Network error: ' + e.message;
                    statusEl.classList.remove('ok');
                    statusEl.classList.add('err');
                }
            }
        }

        // Tracks whether we've already fired the post-upload synthetic
        // chat turn for this conversation. We only fire it once — the
        // FIRST successful upload — so adding/removing more files
        // doesn't keep poking Craig.
        let _uploadAdvanceTriggered = false;

        async function _removeArtwork(idx, wrap) {
            try {
                const resp = await fetch(
                    API_BASE + '/widget/conversations/' + conversationId + '/upload-artwork/' + idx
                    + '?external_id=' + encodeURIComponent(sessionId),
                    { method: 'DELETE' },
                );
                if (!resp.ok) return;
                const result = await resp.json();
                uploadedFiles = result.files || [];
                _renderArtworkCard(wrap);
            } catch (_) { /* swallow */ }
        }

        function _renderArtworkCard(wrap) {
            const filesHtml = uploadedFiles.length
                ? '<div class="jp-upload-list">'
                  + uploadedFiles.map((f, i) => {
                      const sizeMb = (f.size / (1024 * 1024)).toFixed(1);
                      return (
                          '<div class="jp-upload-file" data-idx="' + i + '">'
                          + '  <span class="jp-upload-file-icon">📎</span>'
                          + '  <span class="jp-upload-file-name">' + escapeHtml(f.filename) + '</span>'
                          + '  <span class="jp-upload-file-size">' + sizeMb + ' MB</span>'
                          + '  <button type="button" class="jp-upload-file-remove" data-idx="' + i + '" aria-label="Remove">✕</button>'
                          + '</div>'
                      );
                  }).join('')
                  + '</div>'
                : '';
            const canAddMore = uploadedFiles.length < ARTWORK_MAX_FILES;
            const btnLabel = uploadedFiles.length ? 'Add another' : 'Choose files';
            wrap.innerHTML = (
                '<div class="jp-upload-row">'
                + '  <div class="jp-upload-icon">📎</div>'
                + '  <div class="jp-upload-body">'
                + '    <div class="jp-upload-title">Your artwork</div>'
                + '    <div class="jp-upload-sub">'
                + '      PDF, AI, INDD, JPG, PNG, EPS, TIFF, PSD, SVG up to 100 MB each'
                + (uploadedFiles.length ? ' &middot; ' + uploadedFiles.length + '/' + ARTWORK_MAX_FILES : '')
                + '    </div>'
                + '  </div>'
                + (canAddMore
                    ? '  <button type="button" class="jp-upload-btn" id="jpUploadBtn">' + btnLabel + '</button>'
                      + '  <input type="file" id="jpUploadInput" multiple '
                      + 'accept=".pdf,.ai,.indd,.jpg,.jpeg,.png,.eps,.tiff,.tif,.psd,.svg" '
                      + 'style="display:none;">'
                    : ''
                )
                + '</div>'
                + filesHtml
                + '<div class="jp-upload-status" id="jpUploadStatus" style="display:none;"></div>'
            );
            const btn = document.getElementById('jpUploadBtn');
            const fileInput = document.getElementById('jpUploadInput');
            if (btn && fileInput) {
                btn.addEventListener('click', () => fileInput.click());
                fileInput.addEventListener('change', async () => {
                    for (const f of Array.from(fileInput.files)) {
                        if (uploadedFiles.length >= ARTWORK_MAX_FILES) break;
                        await _uploadOneArtwork(f, wrap);
                    }
                    fileInput.value = '';
                });
            }
            wrap.querySelectorAll('.jp-upload-file-remove').forEach((b) => {
                b.addEventListener('click', async () => {
                    const idx = parseInt(b.getAttribute('data-idx'), 10);
                    await _removeArtwork(idx, wrap);
                });
            });
        }

        function showArtworkUploadButton() {
            // Don't double-render if widget already mounted earlier in chat
            if (document.getElementById('jpUploadCard')) return;
            const wrap = document.createElement('div');
            wrap.className = 'jp-upload-card';
            wrap.id = 'jpUploadCard';
            messagesEl.appendChild(wrap);
            _renderArtworkCard(wrap);
            messagesEl.scrollTop = messagesEl.scrollHeight;
        }

        // ─────────────────────────────────────────────────────────────
        // Phase G v26 — artwork-choice buttons
        //
        // Free-text "do you have artwork or want design?" was a UX
        // dead-end: customers typed "yes I have mine" / "I have it" /
        // "yeah" — all of which the server-side sniffer had to
        // pattern-match. With the explicit two-button choice the
        // answer is unambiguous and the conversation moves forward
        // without typos / clarification rounds.
        // ─────────────────────────────────────────────────────────────
        function showArtworkChoiceButtons() {
            if (document.getElementById('jpArtworkChoice')) return;
            const wrap = document.createElement('div');
            wrap.className = 'jp-choice-card';
            wrap.id = 'jpArtworkChoice';
            wrap.innerHTML = (
                '<button type="button" class="jp-choice-btn" data-choice="have">'
                + '  <div class="jp-choice-title">I have my own artwork</div>'
                + '  <div class="jp-choice-sub">PDF, AI, INDD, JPG, PNG…</div>'
                + '</button>'
                + '<button type="button" class="jp-choice-btn" data-choice="design">'
                + '  <div class="jp-choice-title">I need your design service</div>'
                + '  <div class="jp-choice-sub">Flat €65 ex VAT (€79.95 inc)</div>'
                + '</button>'
            );
            messagesEl.appendChild(wrap);
            messagesEl.scrollTop = messagesEl.scrollHeight;

            wrap.querySelectorAll('.jp-choice-btn').forEach((btn) => {
                btn.addEventListener('click', () => {
                    const choice = btn.getAttribute('data-choice');
                    // Disable both buttons so the customer can't
                    // double-click. Visual feedback happens via the
                    // synthetic chat reply below.
                    wrap.querySelectorAll('.jp-choice-btn').forEach((b) => {
                        b.disabled = true;
                        if (b === btn) b.classList.add('jp-choice-btn-selected');
                        else b.classList.add('jp-choice-btn-faded');
                    });
                    // Show the choice as a user bubble for transcript
                    // clarity, then fire it as a chat turn so the
                    // server-side sniffer stamps the flag.
                    const phrase = choice === 'have'
                        ? "I have my own artwork"
                        : "I need your design service";
                    addMsg(phrase, 'user');
                    sendMessage(phrase);
                });
            });
        }

        function _legacy_unused_showArtworkUploadButton() {
            const wrap = document.createElement('div');
            wrap.className = 'jp-upload-card';
            wrap.id = 'jpUploadCard';
            wrap.innerHTML = (
                '<div class="jp-upload-row">'
                + '  <div class="jp-upload-icon">📎</div>'
                + '  <div class="jp-upload-body">'
                + '    <div class="jp-upload-title">Upload your artwork</div>'
                + '    <div class="jp-upload-sub">PDF, AI, INDD, JPG, PNG… up to 100 MB</div>'
                + '  </div>'
                + '  <button type="button" class="jp-upload-btn" id="jpUploadBtn">Choose file</button>'
                + '  <input type="file" id="jpUploadInput" accept=".pdf,.ai,.indd,.jpg,.jpeg,.png,.eps,.tiff,.tif,.psd,.svg" style="display:none;">'
                + '</div>'
                + '<div class="jp-upload-status" id="jpUploadStatus" style="display:none;"></div>'
            );
            messagesEl.appendChild(wrap);
            messagesEl.scrollTop = messagesEl.scrollHeight;

            const btn = document.getElementById('jpUploadBtn');
            const fileInput = document.getElementById('jpUploadInput');
            const statusEl = document.getElementById('jpUploadStatus');

            btn.addEventListener('click', () => fileInput.click());
            fileInput.addEventListener('change', async () => {
                const file = fileInput.files[0];
                if (!file) return;
                if (file.size > 100 * 1024 * 1024) {
                    statusEl.textContent = 'File too big — max 100 MB.';
                    statusEl.style.display = 'block';
                    statusEl.classList.add('err');
                    return;
                }
                statusEl.classList.remove('err');
                statusEl.style.display = 'block';
                statusEl.textContent = 'Uploading …';
                btn.disabled = true;
                btn.textContent = 'Uploading…';

                const fd = new FormData();
                fd.append('file', file);
                fd.append('external_id', sessionId);

                try {
                    const resp = await fetch(
                        API_BASE + '/widget/conversations/' + conversationId + '/upload-artwork',
                        { method: 'POST', body: fd },
                    );
                    if (!resp.ok) {
                        let msg = 'Upload failed (HTTP ' + resp.status + ').';
                        try {
                            const j = await resp.json();
                            if (j.detail) msg = typeof j.detail === 'string' ? j.detail : JSON.stringify(j.detail);
                        } catch (_) { /* swallow */ }
                        statusEl.textContent = msg;
                        statusEl.classList.add('err');
                        btn.disabled = false;
                        btn.textContent = 'Choose file';
                        return;
                    }
                    const result = await resp.json();
                    uploadedArtwork = result;
                    btn.style.display = 'none';
                    statusEl.classList.remove('err');
                    statusEl.classList.add('ok');
                    const sizeMb = (file.size / (1024 * 1024)).toFixed(1);
                    statusEl.innerHTML = '✓ ' + escapeHtml(file.name) + ' · ' + sizeMb + ' MB';
                } catch (e) {
                    statusEl.textContent = 'Network error: ' + e.message;
                    statusEl.classList.add('err');
                    btn.disabled = false;
                    btn.textContent = 'Choose file';
                }
            });
        }

        function addTyping() {
            const el = document.createElement('div');
            el.className = 'jp-typing';
            el.id = 'jpTyping';
            el.innerHTML = '<span></span><span></span><span></span>';
            messagesEl.appendChild(el);
            messagesEl.scrollTop = messagesEl.scrollHeight;
        }
        function removeTyping() {
            const t = $('jpTyping');
            if (t) t.remove();
        }

        async function sendChat(message, opts = {}) {
            try {
                const res = await fetch(API_BASE + '/chat', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({
                        message: message,
                        conversation_id: conversationId,
                        session_id: sessionId,
                        channel: 'web',
                        organization_slug: CLIENT_SLUG,
                    }),
                });
                // 5xx → server is unhealthy; 429 → rate limited; both surface
                // the banner so the user knows it's a transient infra issue,
                // not Craig being weird at them.
                if (res.status >= 500) {
                    showErrorBanner();
                    return { reply: '', _failed: true };
                }
                if (res.status === 429) {
                    showErrorBanner('Too many messages too fast. Give it a few seconds and try again.');
                    return { reply: '', _failed: true };
                }
                const data = await res.json();
                conversationId = data.conversation_id || conversationId;
                // Server may include an `error` field on otherwise-200 responses
                // (e.g. when the LLM call partially succeeded).
                if (data.error) {
                    showErrorBanner();
                } else {
                    // Success — banner from any prior failure is now stale, hide it.
                    hideErrorBanner();
                }
                return data;
            } catch (e) {
                showErrorBanner();
                return { reply: '', _failed: true };
            }
        }

        // Phase F state: track which markers have already been "consumed"
        // so we don't render the form / upload button twice if the LLM
        // repeats them in subsequent turns.
        let formAlreadyShown = false;
        let uploadAlreadyShown = false;
        let artworkChoiceShown = false;
        let uploadedArtwork = null;  // { url, filename, size }

        async function sendMessage(messageOverride) {
            const text = messageOverride !== undefined ? messageOverride : input.value.trim();
            if (!text && messageOverride === undefined) return;
            const isSynthetic = messageOverride !== undefined;
            if (!isSynthetic) {
                addMsg(text, 'user');
                input.value = '';
            }
            input.disabled = true;
            sendBtn.disabled = true;

            // Unified flow. The server is the single authority on whether a
            // PDF card should render — it emits [QUOTE_READY] only after the
            // customer's contact info has been saved on the conversation.
            // No more client-side "did they say yes" regex racing the LLM.
            addTyping();
            const data = await sendChat(text);
            removeTyping();

            if (data.quote_generated && data.quote_id) {
                lastQuoteId = data.quote_id;
                lastQuoteData = data;
            } else if (typeof data.quote_total_inc_vat === 'number') {
                // Phase G - keep lastQuoteData in sync on every turn that
                // carries a quote total, so the form's dynamic shipping
                // label has fresh data even on turns where no new quote
                // was generated.
                lastQuoteData = data;
                if (data.quote_id) lastQuoteId = data.quote_id;
            }

            const rawReply = data.reply || '';
            const wantsForm = rawReply.indexOf('[CUSTOMER_FORM]') !== -1;
            const wantsUpload = rawReply.indexOf('[ARTWORK_UPLOAD]') !== -1;
            const wantsArtworkChoice = rawReply.indexOf('[ARTWORK_CHOICE]') !== -1;
            // Strip ALL machine markers from what the customer sees.
            const cleanReply = rawReply
                .replace(/\[QUOTE_READY\]/g, '')
                .replace(/\[CUSTOMER_FORM\]/g, '')
                .replace(/\[ARTWORK_UPLOAD\]/g, '')
                .replace(/\[ARTWORK_CHOICE\]/g, '')
                .trim();
            const wantsQuote = rawReply.indexOf('[QUOTE_READY]') !== -1 && lastQuoteId;

            if (cleanReply) addMsg(cleanReply, 'assistant');

            if (wantsArtworkChoice && !artworkChoiceShown) {
                artworkChoiceShown = true;
                showArtworkChoiceButtons();
            }

            if (wantsUpload && !uploadAlreadyShown) {
                uploadAlreadyShown = true;
                showArtworkUploadButton();
            }

            if (wantsForm && !formAlreadyShown) {
                formAlreadyShown = true;
                showCustomerForm();
                // The form takes over; don't re-enable the chat input.
                return;
            }

            if (wantsQuote) {
                // Brief loading animation, then the quote card with View + Download buttons.
                showQuoteLoading();
                await new Promise(function (resolve) { setTimeout(resolve, 1800); });
                removeQuoteLoading();
                showQuoteCard(lastQuoteId);
            }

            if (data.escalated) addMsg("Escalated to Justin \u2014 he'll follow up directly.", 'system');

            input.disabled = false;
            sendBtn.disabled = false;
            input.focus();
        }

        sendBtn.addEventListener('click', sendMessage);
        input.addEventListener('keydown', (e) => {
            if (e.key === 'Enter') sendMessage();
        });

        async function bootChat() {
            // Show the configured greeting immediately — no DeepSeek round-trip
            // for an empty open. The LLM only fires once the customer actually
            // types something, which keeps the widget snappy + saves tokens.
            addMsg(WIDGET_CONFIG.greeting, 'assistant');
            input.focus();
        }

    }

    // wireForm removed — widget is chat-only now
    // Keeping this stub so the closing braces match
    function _removed() {
        const $ = (id) => document.getElementById(id);

        const productSel = $('jpProduct');
        const fieldsEl = $('jpFormFields');
        const quoteBtn = $('jpQuoteBtn');
        const resultEl = $('jpResult');

        // Small-format: which products have quantity tiers
        const SF_QTYS = {
            business_cards: [100, 250, 500, 1000, 2500],
            flyers_a6: [100, 250, 500, 1000, 2500],
            flyers_a5: [100, 250, 500, 1000, 2500],
            flyers_a4: [100, 250, 500, 1000, 2500],
            flyers_dl: [100, 250, 500, 1000, 2500],
            brochures_a4: [100, 250, 500, 1000, 2500],
            compliment_slips: [100, 250, 500, 1000, 2500],
            letterheads: [100, 250, 500, 1000, 2500],
            ncr_pads_a5: [5, 10, 20, 30, 50],
            ncr_pads_a4: [5, 10, 20, 30, 50],
        };

        const SF_FINISHES = {
            business_cards: ['gloss', 'matte', 'soft-touch'],
            flyers_a6: ['gloss', 'matte', 'soft-touch'],
            flyers_a5: ['gloss', 'matte', 'soft-touch'],
            flyers_a4: ['gloss', 'matte', 'soft-touch'],
            flyers_dl: ['gloss', 'matte', 'soft-touch'],
            brochures_a4: ['gloss', 'matte'],
            compliment_slips: ['uncoated'],
            letterheads: ['uncoated'],
            ncr_pads_a5: ['duplicate', 'triplicate'],
            ncr_pads_a4: ['duplicate', 'triplicate'],
        };

        const SF_SIDES_APPLIES = {
            business_cards: true,  // shows control, but engine won't charge extra
            flyers_a6: true, flyers_a5: true, flyers_a4: true, flyers_dl: true,
            brochures_a4: true, compliment_slips: true, letterheads: true,
            ncr_pads_a5: false, ncr_pads_a4: false,
        };

        const BK_PAGES_SS = [8, 12, 16, 20, 24, 28, 32, 36, 40, 44, 48];
        const BK_PAGES_PB = [44, 48, 52, 56, 60, 64, 68, 72, 76, 80, 84, 88, 92, 96];
        const BK_QTYS = [25, 50, 100, 250, 500];

        function renderFields() {
            fieldsEl.innerHTML = '';
            resultEl.innerHTML = '';
            quoteBtn.disabled = true;

            const val = productSel.value;
            if (!val) return;

            const [category, key] = val.split(':');

            if (category === 'sf') {
                const qtys = SF_QTYS[key] || [];
                const finishes = SF_FINISHES[key] || [];
                const showSides = SF_SIDES_APPLIES[key];

                fieldsEl.innerHTML = `
                    <div class="jp-field">
                        <label>Quantity *</label>
                        <select id="jpQty">
                            <option value="">-- Select --</option>
                            ${qtys.map((q) => `<option value="${q}">${q.toLocaleString()}${key.startsWith('ncr') ? ' pads' : ''}</option>`).join('')}
                        </select>
                    </div>
                    <div class="jp-field">
                        <label>Finish *</label>
                        <select id="jpFinish">
                            <option value="">-- Select --</option>
                            ${finishes.map((f) => `<option value="${f}">${f.replace('_', ' ').replace('-', ' ').replace(/\b\w/g, l => l.toUpperCase())}</option>`).join('')}
                        </select>
                    </div>
                    ${showSides ? `
                    <div class="jp-field">
                        <label>Sides</label>
                        <select id="jpSides">
                            <option value="false">Single-sided</option>
                            <option value="true">Double-sided</option>
                        </select>
                    </div>` : ''}
                    <div class="jp-field">
                        <label class="jp-checkbox">
                            <input type="checkbox" id="jpArtwork"> Need design / artwork help (€65+VAT/hr, quoted separately)
                        </label>
                    </div>
                `;
                quoteBtn.disabled = false;
            }
            else if (category === 'lf') {
                const unitLabel = (key === 'pvc_banners' || key === 'window_graphics' ||
                                   key === 'floor_graphics' || key === 'mesh_banners' ||
                                   key === 'fabric_displays' || key === 'vinyl_labels')
                                   ? 'square metres' : 'units';
                fieldsEl.innerHTML = `
                    <div class="jp-field">
                        <label>Quantity (${unitLabel}) *</label>
                        <input type="number" id="jpQty" min="1" step="1" placeholder="e.g. 5">
                    </div>
                    <div class="jp-field">
                        <label class="jp-checkbox">
                            <input type="checkbox" id="jpArtwork"> Need design / artwork help
                        </label>
                    </div>
                `;
                quoteBtn.disabled = false;
            }
            else if (category === 'bk') {
                const [fmt, ...bindingParts] = key.split('_');
                const binding = bindingParts.join('_');
                const pagesList = binding === 'saddle_stitch' ? BK_PAGES_SS : BK_PAGES_PB;
                const covers = binding === 'saddle_stitch'
                    ? ['self_cover', 'card_cover', 'card_cover_lam']
                    : ['card_cover', 'card_cover_lam'];
                const coverLabels = {
                    self_cover: 'Self Cover (150gsm silk)',
                    card_cover: 'Card Cover (300gsm + 150gsm)',
                    card_cover_lam: 'Card Cover + Matt/Gloss Lam',
                };

                fieldsEl.innerHTML = `
                    <div class="jp-field">
                        <label>Pages *</label>
                        <select id="jpPages">
                            <option value="">-- Select --</option>
                            ${pagesList.map((p) => `<option value="${p}">${p}pp</option>`).join('')}
                        </select>
                    </div>
                    <div class="jp-field">
                        <label>Cover Type *</label>
                        <select id="jpCover">
                            <option value="">-- Select --</option>
                            ${covers.map((c) => `<option value="${c}">${coverLabels[c]}</option>`).join('')}
                        </select>
                    </div>
                    <div class="jp-field">
                        <label>Quantity *</label>
                        <select id="jpQty">
                            <option value="">-- Select --</option>
                            ${BK_QTYS.map((q) => `<option value="${q}">${q} copies</option>`).join('')}
                        </select>
                    </div>
                    <div class="jp-field">
                        <label class="jp-checkbox">
                            <input type="checkbox" id="jpArtwork"> Need design help
                        </label>
                    </div>
                `;
                quoteBtn.disabled = false;
            }
        }

        productSel.addEventListener('change', renderFields);

        // --- Submit ---
        quoteBtn.addEventListener('click', async () => {
            const val = productSel.value;
            if (!val) return;
            const [category, key] = val.split(':');

            quoteBtn.disabled = true;
            quoteBtn.querySelector('span').textContent = 'Getting price...';
            resultEl.innerHTML = '';

            let endpoint, body;

            try {
                if (category === 'sf') {
                    const qty = parseInt(document.getElementById('jpQty').value);
                    const finish = document.getElementById('jpFinish').value;
                    if (!qty || !finish) throw new Error('Please complete all required fields.');
                    const sidesEl = document.getElementById('jpSides');
                    const artwork = document.getElementById('jpArtwork').checked;
                    endpoint = '/quote/small-format';
                    body = {
                        product_key: key,
                        quantity: qty,
                        double_sided: sidesEl ? sidesEl.value === 'true' : false,
                        finish: finish,
                        needs_artwork: artwork,
                    };
                } else if (category === 'lf') {
                    const qty = parseInt(document.getElementById('jpQty').value);
                    if (!qty || qty < 1) throw new Error('Please enter a valid quantity.');
                    endpoint = '/quote/large-format';
                    body = {
                        product_key: key,
                        quantity: qty,
                        needs_artwork: document.getElementById('jpArtwork').checked,
                    };
                } else if (category === 'bk') {
                    const [fmt, ...bindingParts] = key.split('_');
                    const binding = bindingParts.join('_');
                    const pages = parseInt(document.getElementById('jpPages').value);
                    const cover = document.getElementById('jpCover').value;
                    const qty = parseInt(document.getElementById('jpQty').value);
                    if (!pages || !cover || !qty) throw new Error('Please complete all required fields.');
                    endpoint = '/quote/booklet';
                    body = {
                        format: fmt,
                        binding: binding,
                        pages: pages,
                        cover_type: cover,
                        quantity: qty,
                        needs_artwork: document.getElementById('jpArtwork').checked,
                    };
                }

                const res = await fetch(API_BASE + endpoint, {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify(body),
                });
                const data = await res.json();
                renderResult(data);
            } catch (e) {
                resultEl.innerHTML = `<div class="jp-result jp-escalation"><h4>Something went wrong</h4><div>${e.message}</div></div>`;
            } finally {
                quoteBtn.disabled = false;
                quoteBtn.querySelector('span').textContent = 'Get price →';
            }
        });

        function renderResult(data) {
            if (!data.success) {
                resultEl.innerHTML = `
                    <div class="jp-result jp-escalation">
                        <h4>That's one for Justin</h4>
                        <div style="color:#040f2a; font-weight:600; margin-bottom:4px;">${data.reason || 'Custom quote needed.'}</div>
                        <div style="color:#6b7a99; font-size:13px;">${data.message || "I'll get Justin to come back to you directly."}</div>
                    </div>
                `;
                return;
            }

            const surcharges = (data.surcharges_applied || [])
                .map((s) => `<span class="jp-chip">${s}</span>`).join('');

            const artworkLine = data.artwork_cost_ex_vat
                ? `<div class="jp-note">+ Artwork: €${data.artwork_cost_ex_vat.toFixed(2)} ex VAT (€${data.artwork_cost_inc_vat.toFixed(2)} inc VAT)</div>`
                : '';

            resultEl.innerHTML = `
                <div class="jp-result">
                    <h4>${data.product_name}</h4>
                    <div class="jp-price">€${data.final_price_ex_vat.toFixed(2)}<span style="font-size:13px;color:#6b7a99;font-weight:500;"> ex VAT</span></div>
                    <div class="jp-price-line">€${data.final_price_inc_vat.toFixed(2)} inc VAT · ${data.turnaround}</div>
                    ${surcharges ? `<div style="margin-bottom:6px;">${surcharges}</div>` : ''}
                    ${artworkLine}
                    <div class="jp-note">Justin will confirm before anything runs.</div>
                </div>
            `;
        }
    }

    // ======================================================================
    // GO
    // ======================================================================
    async function boot() {
        // Fetch tenant branding BEFORE mounting so the initial paint uses the
        // right colors + font. If the fetch fails, defaults apply.
        await fetchWidgetConfig();
        mount();
    }

    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', boot);
    } else {
        boot();
    }
})();
