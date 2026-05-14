"""
Hook Learning System - AI Training and Examples

This system provides examples, documentation, and learning materials for AI
to understand how to create effective hook functions.
"""

from typing import Dict, List, Any
import ast


class HookLearningSystem:
    """System to help AI learn how to create hook functions."""
    
    @staticmethod
    def get_request_object_documentation() -> Dict[str, Any]:
        """Get comprehensive documentation of the request object structure."""
        return {
            "request_object": {
                "description": "The request object passed to hook functions",
                "type": "dict",
                "fields": {
                    "request_id": {
                        "type": "str",
                        "description": "Unique identifier for this request",
                        "example": "fetch-12345-abcde"
                    },
                    "instance_id": {
                        "type": "str", 
                        "description": "Browser instance ID that made the request",
                        "example": "8e226b0c-3879-4d5e-96b3-db1805bfd4c4"
                    },
                    "url": {
                        "type": "str",
                        "description": "Full URL of the request",
                        "example": "https://example.com/api/data?param=value"
                    },
                    "method": {
                        "type": "str",
                        "description": "HTTP method (GET, POST, PUT, DELETE, etc.)",
                        "example": "GET"
                    },
                    "headers": {
                        "type": "dict[str, str]",
                        "description": "Request headers as key-value pairs",
                        "example": {
                            "User-Agent": "Mozilla/5.0...",
                            "Accept": "application/json",
                            "Authorization": "Bearer token123"
                        }
                    },
                    "post_data": {
                        "type": "str or None",
                        "description": "POST/PUT body data (None for GET requests)",
                        "example": '{"username": "user", "password": "pass"}'
                    },
                    "resource_type": {
                        "type": "str or None",
                        "description": "Type of resource (Document, Script, Image, XHR, etc.)",
                        "example": "Document"
                    },
                    "stage": {
                        "type": "str",
                        "description": "Request stage (request or response)",
                        "example": "request"
                    }
                }
            },
            "hook_action": {
                "description": "Return value from hook functions",
                "type": "HookAction or dict",
                "actions": {
                    "continue": {
                        "description": "Allow request to proceed normally",
                        "example": 'HookAction(action="continue")'
                    },
                    "block": {
                        "description": "Block the request entirely",
                        "example": 'HookAction(action="block")'
                    },
                    "redirect": {
                        "description": "Redirect request to a different URL",
                        "fields": ["url"],
                        "example": 'HookAction(action="redirect", url="https://httpbin.org/get")'
                    },
                    "modify": {
                        "description": "Modify request parameters",
                        "fields": ["url", "method", "headers", "post_data"],
                        "example": 'HookAction(action="modify", headers={"X-Custom": "value"})'
                    },
                    "fulfill": {
                        "description": "Return custom response without sending request",
                        "fields": ["status_code", "headers", "body"],
                        "example": 'HookAction(action="fulfill", status_code=200, body="Custom response")'
                    }
                }
            }
        }
    
    @staticmethod 
    def get_hook_examples() -> List[Dict[str, Any]]:
        """Get example hook functions for AI learning."""
        return [
            {
                "name": "Simple URL Blocker",
                "description": "Block all requests to doubleclick.net (ad blocker)",
                "requirements": {
                    "url_pattern": "*doubleclick.net*"
                },
                "function": '''
def process_request(request):
    # Block any request to doubleclick.net
    return HookAction(action="block")
''',
                "explanation": "This hook blocks all requests matching the URL pattern. No conditions needed since we always want to block ads."
            },
            {
                "name": "Simple Redirect",
                "description": "Redirect example.com to httpbin.org for testing",
                "requirements": {
                    "url_pattern": "*example.com*"
                },
                "function": '''
def process_request(request):
    # Redirect to httpbin for testing
    return HookAction(action="redirect", url="https://httpbin.org/get")
''',
                "explanation": "This hook redirects any request to example.com to httpbin.org for testing purposes."
            },
            {
                "name": "Header Modifier",
                "description": "Add custom headers to API requests",
                "requirements": {
                    "url_pattern": "*/api/*"
                },
                "function": '''
def process_request(request):
    # Add API key header to all API requests
    new_headers = request["headers"].copy()
    new_headers["X-API-Key"] = "secret-api-key-123"
    new_headers["X-Custom-Client"] = "Browser-Hook-System"
    
    return HookAction(
        action="modify",
        headers=new_headers
    )
''',
                "explanation": "This hook adds custom headers to API requests. It copies existing headers and adds new ones."
            },
            {
                "name": "Method Converter",
                "description": "Convert GET requests to POST for specific endpoints",
                "requirements": {
                    "url_pattern": "*/convert-to-post*",
                    "method": "GET"
                },
                "function": '''
def process_request(request):
    # Convert GET to POST and add JSON body
    return HookAction(
        action="modify",
        method="POST",
        headers={
            **request["headers"],
            "Content-Type": "application/json"
        },
        post_data='{"converted": true, "original_url": "' + request["url"] + '"}'
    )
''',
                "explanation": "This hook converts GET requests to POST, adds JSON content-type header, and includes original URL in body."
            },
            {
                "name": "Custom Response Generator", 
                "description": "Return custom JSON response for API endpoints",
                "requirements": {
                    "url_pattern": "*/mock-api/*"
                },
                "function": '''
def process_request(request):
    # Return mock API response
    mock_data = {
        "status": "success",
        "data": {
            "message": "This is a mocked response",
            "request_url": request["url"],
            "timestamp": datetime.now().isoformat()
        }
    }
    
    return HookAction(
        action="fulfill",
        status_code=200,
        headers={"Content-Type": "application/json"},
        body=str(mock_data).replace("'", '"')  # Convert to JSON string
    )
''',
                "explanation": "This hook intercepts API requests and returns custom JSON responses without hitting the real server."
            },
            {
                "name": "Conditional Blocker",
                "description": "Block requests based on multiple conditions",
                "requirements": {
                    "url_pattern": "*"  # Match all URLs
                },
                "function": '''
def process_request(request):
    # Block requests to social media trackers during work hours
    social_trackers = ["facebook.com", "twitter.com", "linkedin.com", "instagram.com"]
    
    # Check if URL contains social tracker
    is_social_tracker = any(tracker in request["url"] for tracker in social_trackers)
    
    # Check if it's tracking related
    is_tracker = "/track" in request["url"] or "/analytics" in request["url"]
    
    if is_social_tracker and is_tracker:
        return HookAction(action="block")
    
    # Otherwise continue normally
    return HookAction(action="continue")
''',
                "explanation": "This hook uses conditional logic to block social media trackers based on URL patterns and content."
            },
            {
                "name": "Dynamic URL Rewriter",
                "description": "Rewrite URLs based on patterns and parameters",
                "requirements": {
                    "url_pattern": "*old-domain.com*"
                },
                "function": '''
def process_request(request):
    original_url = request["url"]
    
    # Replace domain but keep path and parameters
    new_url = original_url.replace("old-domain.com", "new-domain.com")
    
    # Add cache-busting parameter
    separator = "&" if "?" in new_url else "?"
    new_url += f"{separator}cache_bust=hook_modified"
    
    return HookAction(action="redirect", url=new_url)
''',
                "explanation": "This hook rewrites URLs by replacing domains and adding parameters, useful for domain migrations."
            },
            {
                "name": "Request Logger",
                "description": "Log specific requests without modifying them",
                "requirements": {
                    "url_pattern": "*important-api*"
                },
                "function": '''
def process_request(request):
    # Log important API calls for debugging
    print(f"[API LOG] {request['method']} {request['url']}")
    
    # Log headers if they contain auth info
    if "authorization" in str(request["headers"]).lower():
        print(f"[API LOG] Has Authorization header")
    
    # Always continue the request
    return HookAction(action="continue")
''',
                "explanation": "This hook logs request details for debugging/monitoring purposes but doesn't modify the request."
            },
            {
                "name": "Security Header Injector",
                "description": "Add security headers to outgoing requests",
                "requirements": {
                    "url_pattern": "*",
                    "custom_condition": "request['method'] in ['POST', 'PUT', 'PATCH']"
                },
                "function": '''
def process_request(request):
    # Add security headers to modification requests
    security_headers = request["headers"].copy()
    security_headers.update({
        "X-Requested-With": "XMLHttpRequest",
        "X-CSRF-Protection": "enabled",
        "X-Custom-Security": "browser-hook-system"
    })
    
    return HookAction(
        action="modify", 
        headers=security_headers
    )
''',
                "explanation": "This hook adds security headers to POST/PUT/PATCH requests using custom conditions in requirements."
            },
            {
                "name": "Response Time Simulator",
                "description": "Add artificial delays by fulfilling with delayed responses",
                "requirements": {
                    "url_pattern": "*slow-api*"
                },
                "function": '''
def process_request(request):
    # Simulate slow API by returning custom response immediately
    # (In real implementation, you'd add actual delays)
    
    return HookAction(
        action="fulfill",
        status_code=200,
        headers={"Content-Type": "application/json"},
        body='{"message": "Simulated slow response", "delay": "3000ms"}'
    )
''',
                "explanation": "This hook simulates slow APIs by immediately returning responses instead of waiting for real server."
            },
            {
                "name": "Response Content Modifier",
                "description": "Modify response content at response stage",
                "requirements": {
                    "url_pattern": "*api/*",
                    "stage": "response"
                },
                "function": '''
def process_request(request):
    # Only process responses (not requests)
    if request.get("stage") != "response":
        return HookAction(action="continue")
    
    # Get response body
    response_body = request.get("response_body", "")
    
    if "user_data" in response_body:
        # Replace sensitive data in API responses
        modified_body = response_body.replace(
            '"email":', '"email_redacted":'
        ).replace(
            '"phone":', '"phone_redacted":'
        )
        
        return HookAction(
            action="fulfill",
            status_code=200,
            headers={"Content-Type": "application/json"},
            body=modified_body
        )
    
    # Continue normally if no modification needed
    return HookAction(action="continue")
''',
                "explanation": "This response-stage hook modifies API response content to redact sensitive user data."
            },
            {
                "name": "Response Header Injector",
                "description": "Add security headers to responses at response stage",
                "requirements": {
                    "url_pattern": "*",
                    "stage": "response"
                },
                "function": '''
def process_request(request):
    # Only process responses
    if request.get("stage") != "response":
        return HookAction(action="continue")
    
    # Add security headers to all responses
    security_headers = {
        "X-Content-Type-Options": "nosniff",
        "X-Frame-Options": "DENY", 
        "X-XSS-Protection": "1; mode=block",
        "Strict-Transport-Security": "max-age=31536000"
    }
    
    # Merge with existing headers
    current_headers = request.get("response_headers", {})
    merged_headers = {**current_headers, **security_headers}
    
    return HookAction(
        action="modify",
        headers=merged_headers
    )
''',
                "explanation": "This response-stage hook adds security headers to all responses for better protection."
            },
            {
                "name": "API Response Faker",
                "description": "Replace API responses with fake data for testing",
                "requirements": {
                    "url_pattern": "*api/users*",
                    "stage": "response"
                },
                "function": '''
def process_request(request):
    # Only process responses
    if request.get("stage") != "response":
        return HookAction(action="continue")
    
    # Generate fake user data for testing
    fake_response = {
        "users": [
            {"id": 1, "name": "Test User 1", "email": "test1@example.com"},
            {"id": 2, "name": "Test User 2", "email": "test2@example.com"},
            {"id": 3, "name": "Test User 3", "email": "test3@example.com"}
        ],
        "total": 3,
        "fake": True
    }
    
    return HookAction(
        action="fulfill",
        status_code=200,
        headers={"Content-Type": "application/json"},
        body=str(fake_response).replace("'", '"')
    )
''',
                "explanation": "This response-stage hook replaces real API responses with fake data for testing environments."
            }
        ]
    
    @staticmethod
    def get_requirements_documentation() -> Dict[str, Any]:
        """Get documentation on hook requirements/matching criteria."""
        return {
            "requirements": {
                "description": "Criteria that determine when a hook should trigger",
                "fields": {
                    "url_pattern": {
                        "type": "str",
                        "description": "Wildcard pattern to match URLs (* = any characters, ? = single character)",
                        "examples": [
                            "*example.com*",  # Any URL containing example.com
                            "https://api.*.com/*",  # Any subdomain of .com domains
                            "*api/v*/users*",  # API versioned endpoints
                            "*.jpg",  # Image files
                            "*doubleclick*"  # Ad networks
                        ]
                    },
                    "method": {
                        "type": "str", 
                        "description": "HTTP method to match (GET, POST, PUT, DELETE, etc.)",
                        "examples": ["GET", "POST", "PUT", "DELETE"]
                    },
                    "resource_type": {
                        "type": "str",
                        "description": "Type of resource to match",
                        "examples": ["Document", "Script", "Image", "XHR", "Fetch", "WebSocket"]
                    },
                    "stage": {
                        "type": "str",
                        "description": "Stage of request processing (request = before sending, response = after receiving headers/body)",
                        "examples": ["request", "response"],
                        "note": "Response stage hooks can access response_body, response_status_code, and response_headers"
                    },
                    "custom_condition": {
                        "type": "str",
                        "description": "Python expression evaluated with 'request' variable",
                        "examples": [
                            "len(request['headers']) > 10",
                            "'json' in request['headers'].get('Content-Type', '')",
                            "request['method'] in ['POST', 'PUT']",
                            "'auth' in request['url'].lower()"
                        ]
                    }
                }
            },
            "best_practices": [
                "Use specific URL patterns to avoid over-matching",
                "Include method filters for POST/PUT hooks to avoid affecting GET requests", 
                "Use custom conditions for complex matching logic",
                "Test hooks with console logging before deploying",
                "Always return a HookAction object",
                "Handle exceptions gracefully",
                "Use priority (lower = higher priority) to control hook execution order"
            ]
        }
    
    @staticmethod
    def get_common_patterns() -> List[Dict[str, Any]]:
        """Get common hook patterns and use cases."""
        return [
            {
                "pattern": "Ad Blocker",
                "requirements": {"url_pattern": "*ads*|*analytics*|*tracking*"},
                "action": "block",
                "use_case": "Block advertising and tracking requests"
            },
            {
                "pattern": "API Proxy", 
                "requirements": {"url_pattern": "*api.old-site.com*"},
                "action": "redirect",
                "use_case": "Redirect API calls to new endpoints"
            },
            {
                "pattern": "Authentication Injector",
                "requirements": {"url_pattern": "*api/*", "method": "GET|POST"},
                "action": "modify",
                "use_case": "Add authentication headers to API requests"
            },
            {
                "pattern": "Mock Server",
                "requirements": {"url_pattern": "*mock/*"},
                "action": "fulfill", 
                "use_case": "Return custom responses for testing"
            },
            {
                "pattern": "Request Logger",
                "requirements": {"url_pattern": "*"},
                "action": "continue",
                "use_case": "Log requests for debugging without modification"
            },
            {
                "pattern": "Security Headers",
                "requirements": {"method": "POST|PUT|PATCH"},
                "action": "modify",
                "use_case": "Add security headers to modification requests"
            }
        ]
    
    @staticmethod
    def validate_hook_function(function_code: str) -> Dict[str, Any]:
        """Validate hook function code for common issues."""
        issues = []
        warnings = []
        
        try:
            # Parse the function code
            parsed = ast.parse(function_code)
            
            # Check for required function
            has_process_request = False
            for node in ast.walk(parsed):
                if isinstance(node, ast.FunctionDef) and node.name == "process_request":
                    has_process_request = True
                    
                    # Check function parameters
                    if len(node.args.args) != 1:
                        issues.append("process_request function must take exactly one parameter (request)")
                    elif node.args.args[0].arg != "request":
                        warnings.append("First parameter should be named 'request' for clarity")
            
            if not has_process_request:
                issues.append("Function must define 'process_request(request)' function")
            
            # Check for dangerous operations
            dangerous_nodes = []
            for node in ast.walk(parsed):
                if isinstance(node, ast.Import) or isinstance(node, ast.ImportFrom):
                    warnings.append(f"Imports may not work in hook context: {ast.dump(node)}")
                elif isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
                    if node.func.id in ['eval', 'exec', 'open', 'input']:
                        issues.append(f"Dangerous function call: {node.func.id}")
            
            return {
                "valid": len(issues) == 0,
                "issues": issues,
                "warnings": warnings
            }
            
        except SyntaxError as e:
            return {
                "valid": False,
                "issues": [f"Syntax error: {e}"],
                "warnings": []
            }
        except Exception as e:
            return {
                "valid": False, 
                "issues": [f"Parse error: {e}"],
                "warnings": []
            }


# Global instance
hook_learning_system = HookLearningSystem()