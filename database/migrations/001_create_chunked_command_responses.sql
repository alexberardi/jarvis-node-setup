-- Migration: Create chunked_command_responses table
-- This table stores chunked responses from commands that generate content incrementally
-- (e.g., story generation, long-form content, etc.)

CREATE TABLE IF NOT EXISTS chunked_command_responses (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    command_name VARCHAR(255) NOT NULL,
    session_id VARCHAR(255) NOT NULL,
    full_content TEXT NOT NULL DEFAULT '',
    created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
);

-- Create indexes for efficient querying
CREATE INDEX IF NOT EXISTS idx_chunked_command_responses_session_id ON chunked_command_responses(session_id);
CREATE INDEX IF NOT EXISTS idx_chunked_command_responses_command_name ON chunked_command_responses(command_name);
CREATE INDEX IF NOT EXISTS idx_chunked_command_responses_updated_at ON chunked_command_responses(updated_at);

-- Create a trigger to automatically update the updated_at timestamp
CREATE OR REPLACE FUNCTION update_updated_at_column()
RETURNS TRIGGER AS $$
BEGIN
    NEW.updated_at = CURRENT_TIMESTAMP;
    RETURN NEW;
END;
$$ language 'plpgsql';

CREATE TRIGGER update_chunked_command_responses_updated_at 
    BEFORE UPDATE ON chunked_command_responses 
    FOR EACH ROW EXECUTE FUNCTION update_updated_at_column();

-- Add comments for documentation
COMMENT ON TABLE chunked_command_responses IS 'Stores chunked responses from commands that generate content incrementally';
COMMENT ON COLUMN chunked_command_responses.id IS 'Unique identifier for the chunked response session';
COMMENT ON COLUMN chunked_command_responses.command_name IS 'Name of the command that generated this response';
COMMENT ON COLUMN chunked_command_responses.session_id IS 'Unique session identifier for this chunked response';
COMMENT ON COLUMN chunked_command_responses.full_content IS 'The complete content generated so far (accumulated chunks)';
COMMENT ON COLUMN chunked_command_responses.created_at IS 'When this chunked response session was first created';
COMMENT ON COLUMN chunked_command_responses.updated_at IS 'When the content was last updated (auto-updated on each change)';
