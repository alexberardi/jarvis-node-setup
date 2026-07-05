"""Update/egress policy keys — the PR #40 consent gates.

These are consent flags, not preferences: they control whether the node
will fetch and execute code from the internet (``allow_updates``), download
model binaries (``wake_word_model_autodownload_enabled``), or which release
channel it follows (``release_track``).

They are writable ONLY via the K2-encrypted config-push channel
(``config_type == "node_config"`` in ``services/config_push_service``),
which command-center cannot forge — the plaintext ``update_node_config``
MQTT channel rejects them (``scripts/mqtt_tts_listener``), because anything
that can publish to the broker could otherwise flip a consent flag.
"""

UPDATE_POLICY_KEYS = frozenset({
    "allow_updates",
    "wake_word_model_autodownload_enabled",
    "release_track",
})

VALID_RELEASE_TRACKS = frozenset({"stable", "dev"})

# Keys whose new value only takes effect at service startup — the wake model
# is only (down)loaded at voice-listener init, so flipping autodownload
# without a restart would look applied while doing nothing.
POLICY_KEYS_REQUIRING_RESTART = frozenset({"wake_word_model_autodownload_enabled"})
