from abc import ABC, abstractmethod
from datetime import datetime, timedelta, timezone
from typing import List, Dict, Any, TYPE_CHECKING
from dataclasses import dataclass

import httpx

from exceptions.missing_secrets_error import MissingSecretsError
from services.secret_service import get_secret_value
from .ijarvis_authentication import AuthenticationConfig
from .ijarvis_parameter import IJarvisParameter
from .ijarvis_secret import IJarvisSecret
from .request_information import RequestInformation
from .command_response import CommandResponse
from .validation_result import ValidationResult
from clients.responses.jarvis_command_center import DateContext

if TYPE_CHECKING:
    from .ijarvis_package import JarvisPackage


@dataclass
class PreRouteResult:
    """Result from a command's pre_route() method.

    arguments: kwargs to pass directly to execute().
    spoken_response: optional override for the spoken TTS message.
        If None, the CommandResponse.context_data["message"] is used.
    """
    arguments: Dict[str, Any]
    spoken_response: str | None = None


@dataclass
class CommandExample:
    """Represents a voice command example with expected parameters"""
    voice_command: str
    expected_parameters: Dict[str, Any]
    is_primary: bool = False


@dataclass
class CommandAntipattern:
    """Represents a command anti-pattern for tool disambiguation"""
    command_name: str
    description: str

class JarvisCommandBase(ABC):
    def execute(self, request_info: RequestInformation, **kwargs) -> CommandResponse:
        """
        Execute the command with request information and parameters

        Args:
            request_info: Information about the request from JCC
            **kwargs: Additional parameters for the command

        Returns:
            CommandResponse object with execution results
        """
        self._validate_secrets()
        self._validate_params(kwargs)

        # Value validation (enums, context-dependent checks)
        results = self.validate_call(**kwargs)
        errors = [r for r in results if not r.success]
        if errors:
            return CommandResponse.validation_error(errors)
        # Apply auto-corrections
        for r in results:
            if r.suggested_value is not None:
                kwargs[r.param_name] = r.suggested_value

        return self.run(request_info, **kwargs)

    def validate_call(self, **kwargs: Any) -> list[ValidationResult]:
        """Validate parameter values before execution.

        Default: loops parameters, calls param.validate() on each.
        Override for cross-param or context-dependent validation
        (e.g., entity_id checked against HA data).
        """
        results: list[ValidationResult] = []
        for param in self.parameters:
            value = kwargs.get(param.name)
            if value is None:
                continue  # Missing params handled by _validate_params
            is_valid, error_msg = param.validate(value)
            if not is_valid:
                results.append(ValidationResult(
                    success=False,
                    param_name=param.name,
                    command_name=self.command_name,
                    message=error_msg,
                    valid_values=param.enum_values,
                ))
        return results

    def _validate_secrets(self):
        missing = []
        for secret in self.required_secrets:
            if secret.required and not get_secret_value(secret.key, secret.scope):
                missing.append(secret.key)
        if missing:
            raise MissingSecretsError(missing)

    def _validate_params(self, kwargs):
        missing = [
            p.name for p in self.parameters if p.required and kwargs.get(p.name) is None
        ]
        if missing:
            raise ValueError(f"Missing required params: {', '.join(missing)}")

    @abstractmethod
    def run(self, **kwargs):
        pass




class IJarvisCommand(JarvisCommandBase, ABC):
    @property
    @abstractmethod
    def command_name(self) -> str:
        """Unique identifier for this command (e.g. 'turn_on_lights', 'check_door_status')"""
        pass

    @property
    @abstractmethod
    def description(self) -> str:
        """Human-readable description of what this command does"""
        pass

    @abstractmethod
    def generate_prompt_examples(self) -> List[CommandExample]:
        """Generate concise examples for prompt/tool registration"""
        pass

    @abstractmethod
    def generate_adapter_examples(self) -> List[CommandExample]:
        """Generate larger, varied examples for adapter training"""
        pass

    @property
    @abstractmethod
    def parameters(self) -> List[IJarvisParameter]:
        """List of parameters this command accepts"""
        pass

    @property
    @abstractmethod
    def required_secrets(self) -> List[IJarvisSecret]:
        pass

    @property
    def all_possible_secrets(self) -> List[IJarvisSecret]:
        """All secrets this command could ever need, across all config variants.

        Used by install_command.py to seed the DB upfront.
        Default: delegates to required_secrets (backward compatible).
        Override when required_secrets is config-dependent.
        """
        return self.required_secrets

    @property
    @abstractmethod
    def keywords(self) -> List[str]:
        """List of keywords that can be used to identify this command (for fuzzy matching)"""
        pass

    @property
    def rules(self) -> List[str]:
        """Optional list of general rules for this command"""
        return []

    @property
    def antipatterns(self) -> List[CommandAntipattern]:
        """Optional list of anti-patterns that point to other commands"""
        return []

    @property
    def allow_direct_answer(self) -> bool:
        """Whether the model may respond directly without calling the tool."""
        return False

    @property
    def critical_rules(self) -> List[str]:
        """Optional list of critical rules that must be followed for this command"""
        return []

    @property
    def required_packages(self) -> List["JarvisPackage"]:
        """
        Python packages this command requires.

        Override to declare pip dependencies for this command.
        Packages are installed on first use and written to custom-requirements.txt.

        Returns:
            List of JarvisPackage declaring pip dependencies
        """
        return []

    @property
    def associated_service(self) -> str | None:
        """Logical service grouping for the mobile settings UI.

        Commands sharing the same associated_service are grouped together
        in the mobile app's settings screen (e.g., "Home Assistant", "OpenWeather").

        When a command has authentication, the auth provider's friendly_name is
        used as the default group. Override this for commands that share config
        but don't have OAuth (e.g., weather, web search).

        Returns:
            Display name for the service group, or None to remain ungrouped
        """
        if self.authentication:
            return self.authentication.friendly_name
        return None

    @property
    def authentication(self) -> AuthenticationConfig | None:
        """Declare OAuth config for commands that need external auth.

        Override to return an AuthenticationConfig describing the OAuth flow.
        Commands sharing a provider (e.g., all HA commands return provider="home_assistant")
        share auth state — once one command stores the secrets, all see them.

        Returns:
            AuthenticationConfig or None if no external auth needed
        """
        return None

    def needs_auth(self) -> bool:
        """Check if auth setup is needed for this command.

        Default logic: if authentication is declared, check that all required
        secrets are present and that no re-auth flag is set in command_auth table.

        Returns:
            True if the mobile app should prompt the user for auth
        """
        if not self.authentication:
            return False
        # Check if required secrets are missing
        for secret in self.required_secrets:
            if secret.required and not get_secret_value(secret.key, secret.scope):
                return True
        # Check command_auth table for 401 / forced re-auth flag
        from services.command_auth_service import get_auth_status
        status = get_auth_status(self.authentication.provider)
        return status.needs_auth if status else False

    def store_auth_values(self, values: dict[str, str]) -> None:
        """Called when auth tokens are delivered from mobile via config push.

        Override to process and store the received auth values as secrets.
        For example, an HA command might create a long-lived access token
        from the short-lived OAuth token, then store HA URLs and the LLAT.

        Args:
            values: Dict of auth values. Keys match AuthenticationConfig.keys,
                    plus "_base_url" if discovery was used.
        """
        pass

    def refresh_token(self) -> bool:
        """Refresh OAuth2 access token using the standard refresh_token grant.

        Default implementation POSTs to ``auth.exchange_url`` with
        ``grant_type=refresh_token``, stores the new tokens via
        ``store_auth_values()``, and persists ``TOKEN_EXPIRES_AT_<PROVIDER>``
        in the secret DB.

        Commands with non-standard refresh flows should override this method.

        Returns:
            True if the refresh succeeded, False otherwise.
        """
        from jarvis_log_client import JarvisLogger
        from services.secret_service import set_secret

        logger = JarvisLogger(service="jarvis-node")
        auth = self.authentication
        if not auth or not auth.exchange_url or not auth.refresh_token_secret_key:
            return False

        current_refresh = get_secret_value(auth.refresh_token_secret_key, "integration")
        if not current_refresh:
            logger.warning(
                "No refresh token stored — flagging re-auth",
                provider=auth.provider,
            )
            from services.command_auth_service import set_needs_auth
            set_needs_auth(auth.provider, "No refresh token available")
            return False

        payload: dict[str, str] = {
            "grant_type": "refresh_token",
            "refresh_token": current_refresh,
            "client_id": auth.client_id,
        }

        # Some providers require client_secret
        client_secret = get_secret_value(
            f"{auth.provider.upper()}_CLIENT_SECRET", "integration"
        )
        if client_secret:
            payload["client_secret"] = client_secret

        try:
            resp = httpx.post(auth.exchange_url, data=payload, timeout=15.0)

            if resp.status_code in (400, 401):
                logger.warning(
                    "Token refresh failed — flagging re-auth",
                    status_code=resp.status_code,
                    provider=auth.provider,
                )
                from services.command_auth_service import set_needs_auth
                set_needs_auth(auth.provider, f"Refresh failed: HTTP {resp.status_code}")
                return False

            resp.raise_for_status()
            data = resp.json()

            # Build values dict matching AuthenticationConfig.keys
            values: dict[str, str] = {}
            for key in auth.keys:
                if key in data:
                    values[key] = data[key]

            if values:
                self.store_auth_values(values)

            # Store expires_at
            expires_in = data.get("expires_in")
            if expires_in:
                expires_at = datetime.now(timezone.utc) + timedelta(seconds=int(expires_in))
                set_secret(
                    f"TOKEN_EXPIRES_AT_{auth.provider.upper()}",
                    expires_at.isoformat(),
                    "integration",
                )

            logger.info("Token refreshed", provider=auth.provider)
            return True

        except Exception as e:
            logger.error("Token refresh failed", provider=auth.provider, error=str(e))
            return False

    def pre_route(self, voice_command: str) -> PreRouteResult | None:
        """Deterministic matching — bypass the command center entirely.

        Override to claim short/unambiguous utterances that don't need LLM
        inference (e.g. "pause", "skip", "volume 50").

        Returns:
            PreRouteResult with arguments for execute(), or None to fall
            through to the normal LLM path.
        """
        return None

    def post_process_tool_call(self, args: Dict[str, Any], voice_command: str) -> Dict[str, Any]:
        """Fix up LLM tool-call arguments before execution.

        Called after the LLM produces a tool call but before execute().
        Override to patch common LLM mistakes (e.g. missing query field).

        Returns:
            The (possibly modified) arguments dict.
        """
        return args

    def handle_action(self, action_name: str, context: Dict[str, Any]) -> CommandResponse:
        """Handle an interactive action triggered by a button tap in the mobile app.

        Called when the user taps an action button (e.g. Send, Cancel) on a
        response that included actions. Override to implement action handling.

        The base implementation handles ``cancel_click`` automatically so that
        individual commands don't need to re-implement cancellation.

        Args:
            action_name: The action identifier (e.g. "send_click", "cancel_click").
            context: Context data from the original response (e.g. draft contents).

        Returns:
            CommandResponse with the result of the action.
        """
        if action_name == "cancel_click":
            return CommandResponse.final_response(
                context_data={"cancelled": True, "message": "Cancelled."}
            )
        return CommandResponse.error_response(
            error_details=f"Action '{action_name}' is not supported by {self.command_name}."
        )

    def init_data(self) -> Dict[str, Any]:
        """
        Optional initialization hook. Called manually on first install.

        Override to sync data on first install:
        - Register devices with Command Center
        - Fetch initial state from external services
        - Set up integrations

        Returns:
            Dict with initialization results (for logging/display)

        Usage:
            python scripts/init_data.py --command <command_name>
        """
        return {"status": "no_init_required"}

    @abstractmethod
    def run(self, request_info: RequestInformation, **kwargs) -> CommandResponse:
        """
        Execute the command with request information and parameters
        
        Args:
            request_info: Information about the request from JCC
            **kwargs: Additional parameters for the command
            
        Returns:
            CommandResponse object with:
            - context_data: Raw data for the server to use in generating response
            - success: Whether the command succeeded
            - error_details: Any error information
            - wait_for_input: Whether to wait for follow-up questions
        """
        pass

    def _validate_examples(self, examples: List[CommandExample]) -> None:
        """Validate that examples follow the rules"""
        primary_count = sum(1 for ex in examples if ex.is_primary)
        if primary_count > 1:
            raise ValueError(f"Command '{self.command_name}' has {primary_count} primary examples. Only 0 or 1 allowed.")
    
    def get_command_schema(self, date_context: DateContext, use_adapter_examples: bool = False) -> Dict[str, Any]:
        """Generate the command schema for the LLM"""
        examples = self.generate_adapter_examples() if use_adapter_examples else self.generate_prompt_examples()
        self._validate_examples(examples)
        
        schema = {
            "command_name": self.command_name,
            "description": self.description,
            "allow_direct_answer": self.allow_direct_answer,
            "examples": [
                {
                    "voice_command": ex.voice_command,
                    "expected_parameters": ex.expected_parameters,
                    "is_primary": ex.is_primary
                }
                for ex in examples
            ],
            "keywords": self.keywords,
            "parameters": [param.to_dict() for param in self.parameters]
        }
        
        # Add rules if they exist
        if self.rules:
            schema["rules"] = self.rules
        
        # Add antipatterns if they exist
        if self.antipatterns:
            schema["antipatterns"] = [
                {
                    "command_name": antipattern.command_name,
                    "description": antipattern.description
                }
                for antipattern in self.antipatterns
            ]

        # Add critical rules if they exist
        if self.critical_rules:
            schema["critical_rules"] = self.critical_rules
            
        return schema
    
    def get_primary_example(self, date_context: DateContext) -> CommandExample:
        """Get the primary example for command inference (or first if none marked primary)"""
        examples = self.generate_prompt_examples()
        self._validate_examples(examples)
        
        # Find primary example
        primary_examples = [ex for ex in examples if ex.is_primary]
        if primary_examples:
            return primary_examples[0]
        
        # If no primary, return first example
        if examples:
            return examples[0]
        
        raise ValueError(f"Command '{self.command_name}' has no examples")

    def validate_secrets(self):
        missing = []
        for secret in self.required_secrets:
            if secret.required and not get_secret_value(secret.key, secret.scope):
                missing.append(secret.key)

        if missing:
            raise MissingSecretsError(missing)
    
    def to_openai_tool_schema(self, date_context: DateContext) -> Dict[str, Any]:
        """
        Convert this command to OpenAI function/tool calling schema format
        
        Args:
            date_context: Date context for generating examples
            
        Returns:
            Dictionary in OpenAI tool schema format
        """
        examples = self.generate_prompt_examples()
        self._validate_examples(examples)


        # Map our parameter types to JSON Schema types
        type_mapping = {
            'str': 'string', 'string': 'string',
            'int': 'integer', 'integer': 'integer',
            'float': 'number',
            'bool': 'boolean', 'boolean': 'boolean',
            'list': 'array', 'array': 'array',
            'dict': 'object',
            'datetime': 'string',  # ISO datetime strings
            'date': 'string',  # ISO date strings
            'time': 'string',  # ISO time strings
            'array[datetime]': 'array',
            'array[date]': 'array',
            'array<datetime>': 'array',
            'array<date>': 'array',
            'datetime[]': 'array',
            'date[]': 'array',
        }
        
        properties = {}
        required_params = []
        
        for param in self.parameters:
            json_type = type_mapping.get(param.param_type, 'string')
            
            param_schema = {
                "type": json_type
            }
            
            if param.description:
                param_schema["description"] = param.description
            
            if param.enum_values:
                param_schema["enum"] = param.enum_values

            # Handle array types
            if param.param_type.startswith('array') or param.param_type.endswith('[]'):
                param_schema["type"] = "array"
                if 'datetime' in param.param_type:
                    param_schema["items"] = {"type": "string", "format": "date-time"}
                elif 'date' in param.param_type:
                    param_schema["items"] = {"type": "string", "format": "date"}

            if getattr(param, 'refinable', False):
                param_schema["_refinable"] = True

            properties[param.name] = param_schema
            
            if param.required:
                required_params.append(param.name)
        
        tool_schema = {
            "type": "function",
            "function": {
                "name": self.command_name,
                "description": self.description,
                "parameters": {
                    "type": "object",
                    "properties": properties,
                    "required": required_params
                }
            },
            "allow_direct_answer": self.allow_direct_answer,
            "keywords": self.keywords,
            # Include examples to help the server-side model during warmup
            "examples": [
                {
                    "voice_command": ex.voice_command,
                    "expected_parameters": ex.expected_parameters,
                    "is_primary": ex.is_primary
                } for ex in examples
            ]
        }

        if self.antipatterns:
            tool_schema["antipatterns"] = [
                {
                    "command_name": antipattern.command_name,
                    "description": antipattern.description
                }
                for antipattern in self.antipatterns
            ]

        return tool_schema


