/**
 * Tour Booking Widget — embeddable chat assistant.
 *
 * Usage: <script src="https://widget.lastminutedealshq.com/widget.js?op=oturista" async></script>
 *
 * Shadow DOM for style isolation. No dependencies. ~35KB.
 * SSE streaming for AI responses. Stripe Checkout in new tab.
 */
(function() {
    'use strict';

    var API_URL = '__WIDGET_API_URL__';
    var OPERATOR_ID = '__OPERATOR_ID__';

    // Extract operator from script tag query param if not injected
    if (!OPERATOR_ID || OPERATOR_ID.indexOf('__') === 0) {
        var scripts = document.querySelectorAll('script[src*="widget.js"]');
        for (var i = 0; i < scripts.length; i++) {
            var src = scripts[i].getAttribute('src') || '';
            var match = src.match(/[?&]op=([^&]+)/);
            if (match) { OPERATOR_ID = match[1]; break; }
        }
    }
    if (!API_URL || API_URL.indexOf('__') === 0) {
        API_URL = 'https://widget.lastminutedealshq.com';
    }

    var SESSION_KEY = 'widget_session_' + OPERATOR_ID;
    var sessionToken = localStorage.getItem(SESSION_KEY) || '';
    var conversationId = '';
    var isOpen = false;
    var isLoading = false;
    var branding = { primary_color: '#1a5632', bubble_text: 'Ask about tours!' };

    // ── Create Shadow DOM container ────────────────────────────────────────
    var host = document.createElement('div');
    host.id = 'tour-widget-host';
    document.body.appendChild(host);
    var shadow = host.attachShadow({ mode: 'closed' });

    var STYLES = '\
        * { margin: 0; padding: 0; box-sizing: border-box; }\
        .widget-bubble {\
            position: fixed; bottom: 24px; right: 24px; width: 60px; height: 60px;\
            border-radius: 50%; background: var(--primary, #1a5632); color: white;\
            display: flex; align-items: center; justify-content: center;\
            cursor: pointer; box-shadow: 0 4px 16px rgba(0,0,0,0.2);\
            z-index: 999999; transition: transform 0.2s; font-size: 28px;\
        }\
        .widget-bubble:hover { transform: scale(1.08); }\
        .widget-panel {\
            position: fixed; bottom: 96px; right: 24px; width: 380px; height: 600px;\
            background: white; border-radius: 16px; display: none; flex-direction: column;\
            box-shadow: 0 8px 32px rgba(0,0,0,0.15); z-index: 999998; overflow: hidden;\
        }\
        .widget-panel.open { display: flex; }\
        @media (max-width: 768px) {\
            .widget-panel {\
                bottom: 0; right: 0; left: 0; width: 100%; height: 85vh;\
                border-radius: 16px 16px 0 0;\
            }\
        }\
        .widget-header {\
            background: var(--primary, #1a5632); color: white; padding: 16px 20px;\
            display: flex; align-items: center; justify-content: space-between;\
        }\
        .widget-header-title { font-size: 16px; font-weight: 600; font-family: -apple-system, BlinkMacSystemFont, sans-serif; }\
        .widget-close {\
            background: none; border: none; color: white; font-size: 24px;\
            cursor: pointer; padding: 0 4px; line-height: 1;\
        }\
        .widget-messages {\
            flex: 1; overflow-y: auto; padding: 16px; font-family: -apple-system, BlinkMacSystemFont, sans-serif;\
            font-size: 14px; line-height: 1.6;\
        }\
        .msg { margin-bottom: 12px; display: flex; }\
        .msg-user { justify-content: flex-end; }\
        .msg-assistant { justify-content: flex-start; }\
        .msg-bubble {\
            max-width: 80%; padding: 10px 14px; border-radius: 12px;\
            word-wrap: break-word; white-space: pre-wrap;\
        }\
        .msg-user .msg-bubble { background: var(--primary, #1a5632); color: white; border-bottom-right-radius: 4px; }\
        .msg-assistant .msg-bubble { background: #f3f4f6; color: #1f2937; border-bottom-left-radius: 4px; }\
        .msg-system {\
            text-align: center; font-size: 12px; color: #9ca3af; margin: 8px 0;\
        }\
        .checkout-btn {\
            display: inline-block; margin-top: 8px; padding: 10px 20px;\
            background: var(--primary, #1a5632); color: white; border: none;\
            border-radius: 8px; cursor: pointer; font-size: 14px; font-weight: 600;\
            text-decoration: none;\
        }\
        .checkout-btn:hover { opacity: 0.9; }\
        .widget-input-area {\
            display: flex; padding: 12px 16px; border-top: 1px solid #e5e7eb;\
            background: white;\
        }\
        .widget-input {\
            flex: 1; border: 1px solid #d1d5db; border-radius: 8px; padding: 10px 14px;\
            font-size: 14px; outline: none; font-family: inherit; resize: none;\
        }\
        .widget-input:focus { border-color: var(--primary, #1a5632); }\
        .widget-send {\
            margin-left: 8px; background: var(--primary, #1a5632); color: white;\
            border: none; border-radius: 8px; padding: 10px 16px; cursor: pointer;\
            font-size: 14px; font-weight: 600;\
        }\
        .widget-send:disabled { opacity: 0.5; cursor: not-allowed; }\
        .typing-indicator { display: flex; gap: 4px; padding: 8px 0; }\
        .typing-dot {\
            width: 8px; height: 8px; border-radius: 50%; background: #9ca3af;\
            animation: typing 1.2s ease-in-out infinite;\
        }\
        .typing-dot:nth-child(2) { animation-delay: 0.2s; }\
        .typing-dot:nth-child(3) { animation-delay: 0.4s; }\
        @keyframes typing { 0%, 60%, 100% { opacity: 0.3; } 30% { opacity: 1; } }\
        .privacy-note { font-size: 11px; color: #9ca3af; text-align: center; padding: 4px 16px 8px; line-height: 1.4; }\
        .privacy-note a { color: #6b7280; }\
    ';

    var styleEl = document.createElement('style');
    styleEl.textContent = STYLES;
    shadow.appendChild(styleEl);

    // ── Build DOM ──────────────────────────────────────────────────────────
    var bubble = document.createElement('div');
    bubble.className = 'widget-bubble';
    bubble.innerHTML = '💬';
    bubble.setAttribute('aria-label', 'Open chat');
    shadow.appendChild(bubble);

    var panel = document.createElement('div');
    panel.className = 'widget-panel';
    panel.innerHTML = '\
        <div class="widget-header">\
            <span class="widget-header-title">Tour Assistant</span>\
            <button class="widget-close" aria-label="Close">&times;</button>\
        </div>\
        <div class="widget-messages" id="widget-messages"></div>\
        <div class="privacy-note">AI-powered assistant. <a href="' + API_URL + '/privacy" target="_blank">Privacy Policy</a></div>\
        <div class="widget-input-area">\
            <textarea class="widget-input" id="widget-input" placeholder="Ask about tours..." rows="1"></textarea>\
            <button class="widget-send" id="widget-send">Send</button>\
        </div>\
    ';
    shadow.appendChild(panel);

    var messagesEl = panel.querySelector('#widget-messages');
    var inputEl = panel.querySelector('#widget-input');
    var sendBtn = panel.querySelector('#widget-send');
    var closeBtn = panel.querySelector('.widget-close');

    // ── Events ─────────────────────────────────────────────────────────────
    bubble.addEventListener('click', function() { togglePanel(true); });
    closeBtn.addEventListener('click', function() { togglePanel(false); });

    sendBtn.addEventListener('click', sendMessage);
    inputEl.addEventListener('keydown', function(e) {
        if (e.key === 'Enter' && !e.shiftKey) {
            e.preventDefault();
            sendMessage();
        }
    });

    function togglePanel(open) {
        isOpen = open;
        panel.classList.toggle('open', open);
        bubble.style.display = open ? 'none' : 'flex';
        if (open && !sessionToken) initSession();
        if (open) inputEl.focus();
    }

    // ── Session ────────────────────────────────────────────────────────────
    function initSession() {
        fetch(API_URL + '/api/session', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ operator_id: OPERATOR_ID }),
        })
        .then(function(r) { return r.json(); })
        .then(function(data) {
            sessionToken = data.session_token;
            conversationId = data.conversation_id;
            localStorage.setItem(SESSION_KEY, sessionToken);
            if (data.branding) {
                branding = data.branding;
                host.style.setProperty('--primary', branding.primary_color);
            }
            if (data.welcome_message) {
                addMessage('assistant', data.welcome_message);
            }
        })
        .catch(function(e) {
            addMessage('system', 'Unable to connect. Please try again.');
        });
    }

    // ── Send message ───────────────────────────────────────────────────────
    function sendMessage() {
        var text = inputEl.value.trim();
        if (!text || isLoading) return;

        inputEl.value = '';
        addMessage('user', text);
        setLoading(true);

        fetch(API_URL + '/api/chat', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
                operator_id: OPERATOR_ID,
                session_token: sessionToken,
                message: text,
            }),
        })
        .then(function(r) { return r.json(); })
        .then(function(data) {
            setLoading(false);
            if (data.message) {
                addMessage('assistant', data.message);
            }
            if (data.checkout) {
                // AI triggered checkout — need to create Stripe session
                createCheckout(data.checkout);
            }
            if (data.session_token && data.session_token !== sessionToken) {
                sessionToken = data.session_token;
                localStorage.setItem(SESSION_KEY, sessionToken);
            }
        })
        .catch(function(e) {
            setLoading(false);
            addMessage('system', 'Something went wrong. Please try again.');
        });
    }

    // ── Checkout ────────────────────────────────────────────────────────────
    function createCheckout(checkoutData) {
        fetch(API_URL + '/api/checkout', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
                operator_id: OPERATOR_ID,
                session_token: sessionToken,
                product_id: checkoutData.product_id,
                option_id: checkoutData.option_id,
                availability_id: checkoutData.availability_id,
                unit_id: checkoutData.unit_id,
                quantity: checkoutData.quantity,
                customer_name: checkoutData.customer_name,
                customer_email: checkoutData.customer_email,
                customer_phone: checkoutData.customer_phone || '',
                start_time: checkoutData.start_time || '',
                pickup_location: checkoutData.pickup_location || '',
            }),
        })
        .then(function(r) { return r.json(); })
        .then(function(data) {
            if (data.checkout_url) {
                var sym = data.currency_symbol || '\u20AC';
                var total = data.total_price || 0;
                addCheckoutButton(sym + total.toFixed(0), data.checkout_url);
                startPolling();
            } else if (data.error) {
                addMessage('system', data.error);
            }
        })
        .catch(function() {
            addMessage('system', 'Could not create checkout. Please try again.');
        });
    }

    function addCheckoutButton(label, url) {
        var div = document.createElement('div');
        div.className = 'msg msg-assistant';
        var bub = document.createElement('div');
        bub.className = 'msg-bubble';
        var btn = document.createElement('a');
        btn.className = 'checkout-btn';
        btn.href = url;
        btn.target = '_blank';
        btn.textContent = 'Pay ' + label;
        bub.appendChild(btn);
        div.appendChild(bub);
        messagesEl.appendChild(div);
        scrollToBottom();
    }

    // ── Poll for booking confirmation ──────────────────────────────────────
    var pollTimer = null;
    var pollAttempts = 0;

    function startPolling() {
        pollAttempts = 0;
        if (pollTimer) clearInterval(pollTimer);
        pollTimer = setInterval(function() {
            pollAttempts++;
            if (pollAttempts > 60) { clearInterval(pollTimer); return; }
            fetch(API_URL + '/api/conversation/' + sessionToken + '/status')
                .then(function(r) { return r.json(); })
                .then(function(data) {
                    if (data.state === 'confirmed') {
                        clearInterval(pollTimer);
                        addMessage('system', '\u2705 Booking confirmed! Reference: ' + (data.booking_id || ''));
                    } else if (data.state === 'checkout' && data.context && data.context.error) {
                        clearInterval(pollTimer);
                        addMessage('system', '\u26a0\ufe0f ' + data.context.error);
                    }
                })
                .catch(function() {});
        }, 3000);
    }

    // ── DOM helpers ────────────────────────────────────────────────────────
    function addMessage(role, text) {
        var div = document.createElement('div');
        if (role === 'system') {
            div.className = 'msg-system';
            div.textContent = text;
        } else {
            div.className = 'msg msg-' + role;
            var bub = document.createElement('div');
            bub.className = 'msg-bubble';
            bub.textContent = text;
            div.appendChild(bub);
        }
        messagesEl.appendChild(div);
        scrollToBottom();
    }

    function setLoading(loading) {
        isLoading = loading;
        sendBtn.disabled = loading;
        var existing = messagesEl.querySelector('.typing-indicator');
        if (loading && !existing) {
            var dots = document.createElement('div');
            dots.className = 'msg msg-assistant';
            var bub = document.createElement('div');
            bub.className = 'msg-bubble typing-indicator';
            bub.innerHTML = '<div class="typing-dot"></div><div class="typing-dot"></div><div class="typing-dot"></div>';
            dots.appendChild(bub);
            messagesEl.appendChild(dots);
            scrollToBottom();
        } else if (!loading && existing) {
            existing.closest('.msg').remove();
        }
    }

    function scrollToBottom() {
        messagesEl.scrollTop = messagesEl.scrollHeight;
    }

    // Auto-restore session — show welcome message if no messages visible
    if (sessionToken && messagesEl && messagesEl.children.length === 0) {
        // Session exists but no messages shown — fetch welcome message
        fetch(API_URL + '/api/session', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ operator_id: OPERATOR_ID }),
        })
        .then(function(r) { return r.json(); })
        .then(function(data) {
            if (data.welcome_message && messagesEl.children.length === 0) {
                addMessage('assistant', data.welcome_message);
            }
        })
        .catch(function() {});
    }
})();
