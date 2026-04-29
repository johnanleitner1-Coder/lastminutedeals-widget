-- Widget conversation and event tables
-- Run against Supabase Postgres (from Railway or Supabase SQL Editor)

CREATE TABLE IF NOT EXISTS widget_conversations (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  operator_id TEXT NOT NULL,
  channel TEXT NOT NULL DEFAULT 'web',
  session_token TEXT UNIQUE,
  whatsapp_phone TEXT,
  messages JSONB NOT NULL DEFAULT '[]'::jsonb,
  state TEXT NOT NULL DEFAULT 'greeting',
  context JSONB DEFAULT '{}'::jsonb,
  created_at TIMESTAMPTZ DEFAULT now(),
  updated_at TIMESTAMPTZ DEFAULT now(),
  converted BOOLEAN DEFAULT false,
  booking_id TEXT,
  revenue_cents INTEGER,
  message_count INTEGER DEFAULT 0,
  referrer TEXT,
  user_agent TEXT
);

CREATE TABLE IF NOT EXISTS widget_events (
  id BIGSERIAL PRIMARY KEY,
  operator_id TEXT NOT NULL,
  conversation_id UUID REFERENCES widget_conversations(id),
  event_type TEXT NOT NULL,
  metadata JSONB DEFAULT '{}'::jsonb,
  created_at TIMESTAMPTZ DEFAULT now()
);

-- Indexes
CREATE INDEX IF NOT EXISTS idx_wc_operator ON widget_conversations(operator_id);
CREATE INDEX IF NOT EXISTS idx_wc_session ON widget_conversations(session_token);
CREATE INDEX IF NOT EXISTS idx_wc_whatsapp ON widget_conversations(whatsapp_phone);
CREATE INDEX IF NOT EXISTS idx_wc_created ON widget_conversations(created_at);
CREATE INDEX IF NOT EXISTS idx_we_operator ON widget_events(operator_id);
CREATE INDEX IF NOT EXISTS idx_we_conv ON widget_events(conversation_id);
CREATE INDEX IF NOT EXISTS idx_we_type ON widget_events(event_type);
CREATE INDEX IF NOT EXISTS idx_we_created ON widget_events(created_at);

-- Enable RLS (Row Level Security) but allow service role full access
ALTER TABLE widget_conversations ENABLE ROW LEVEL SECURITY;
ALTER TABLE widget_events ENABLE ROW LEVEL SECURITY;

-- Service role bypass policies
CREATE POLICY IF NOT EXISTS "Service role full access on conversations"
  ON widget_conversations FOR ALL
  USING (true) WITH CHECK (true);

CREATE POLICY IF NOT EXISTS "Service role full access on events"
  ON widget_events FOR ALL
  USING (true) WITH CHECK (true);
