"""
Response Stage Hook Processing - Extension for Dynamic Hook System

This module adds response-stage interception and modification capabilities
to the existing dynamic hook system. It allows AI-generated functions to
modify response content, headers, and status codes.
"""

import asyncio
from typing import Dict, Any, Optional
import nodriver as uc
from debug_logger import debug_logger
from dynamic_hook_system import HookAction, RequestInfo


class ResponseStageProcessor:
    """Handles response-stage hook processing with body modification."""
    
    def __init__(self, dynamic_hook_system):
        self.dynamic_hook_system = dynamic_hook_system
    
    async def execute_response_action(self, tab, request: RequestInfo, action: HookAction, event) -> None:
        """Execute a response-stage hook action."""
        try:
            request_id = uc.cdp.fetch.RequestId(request.request_id)
            
            if action.action == "block":
                # Block at response stage means fail the request
                await tab.send(uc.cdp.fetch.fail_request(
                    request_id=request_id,
                    error_reason=uc.cdp.network.ErrorReason.BLOCKED_BY_CLIENT
                ))
                debug_logger.log_info("response_stage", "execute_response_action", f"Blocked response for {request.url}")
            
            elif action.action == "fulfill":
                # Custom response - override the server response
                headers = []
                if action.headers:
                    for name, value in action.headers.items():
                        headers.append(uc.cdp.fetch.HeaderEntry(name=name, value=value))
                
                await tab.send(uc.cdp.fetch.fulfill_request(
                    request_id=request_id,
                    response_code=action.status_code or 200,
                    response_headers=headers,
                    body=action.body or ""
                ))
                debug_logger.log_info("response_stage", "execute_response_action", f"Fulfilled response for {request.url} with custom content")
            
            elif action.action == "modify":
                # Modify response headers and/or status code
                response_headers = []
                if action.headers:
                    for name, value in action.headers.items():
                        response_headers.append(uc.cdp.fetch.HeaderEntry(name=name, value=value))
                
                await tab.send(uc.cdp.fetch.continue_response(
                    request_id=request_id,
                    response_code=action.status_code,
                    response_headers=response_headers if response_headers else None
                ))
                debug_logger.log_info("response_stage", "execute_response_action", f"Modified response headers/status for {request.url}")
            
            else:
                # Continue response normally
                await tab.send(uc.cdp.fetch.continue_response(request_id=request_id))
                debug_logger.log_info("response_stage", "execute_response_action", f"Continued response normally for {request.url}")
                
        except Exception as e:
            debug_logger.log_error("response_stage", "execute_response_action", f"Error executing response action: {e}")
            # Continue response on error
            try:
                await tab.send(uc.cdp.fetch.continue_response(request_id=uc.cdp.fetch.RequestId(request.request_id)))
            except:
                pass
    
    async def execute_request_action(self, tab, request: RequestInfo, action: HookAction) -> None:
        """Execute a request-stage hook action."""
        try:
            request_id = uc.cdp.fetch.RequestId(request.request_id)
            
            if action.action == "block":
                await tab.send(uc.cdp.fetch.fail_request(
                    request_id=request_id,
                    error_reason=uc.cdp.network.ErrorReason.BLOCKED_BY_CLIENT
                ))
                debug_logger.log_info("response_stage", "execute_request_action", f"Blocked request {request.url}")
            
            elif action.action == "redirect":
                await tab.send(uc.cdp.fetch.continue_request(
                    request_id=request_id,
                    url=action.url
                ))
                debug_logger.log_info("response_stage", "execute_request_action", f"Redirected {request.url} to {action.url}")
            
            elif action.action == "fulfill":
                # Custom response at request stage
                headers = []
                if action.headers:
                    for name, value in action.headers.items():
                        headers.append(uc.cdp.fetch.HeaderEntry(name=name, value=value))
                
                await tab.send(uc.cdp.fetch.fulfill_request(
                    request_id=request_id,
                    response_code=action.status_code or 200,
                    response_headers=headers,
                    body=action.body or ""
                ))
                debug_logger.log_info("response_stage", "execute_request_action", f"Fulfilled request {request.url}")
            
            elif action.action == "modify":
                # Modify request parameters
                headers = []
                if action.headers:
                    for name, value in action.headers.items():
                        headers.append(uc.cdp.fetch.HeaderEntry(name=name, value=value))
                
                await tab.send(uc.cdp.fetch.continue_request(
                    request_id=request_id,
                    url=action.url or request.url,
                    method=action.method or request.method,
                    headers=headers if headers else None,
                    post_data=action.post_data
                ))
                debug_logger.log_info("response_stage", "execute_request_action", f"Modified request {request.url}")
            
            else:
                # Continue request normally
                await tab.send(uc.cdp.fetch.continue_request(request_id=request_id))
                debug_logger.log_info("response_stage", "execute_request_action", f"Continued request {request.url}")
                
        except Exception as e:
            debug_logger.log_error("response_stage", "execute_request_action", f"Error executing request action: {e}")
            # Continue request on error
            try:
                await tab.send(uc.cdp.fetch.continue_request(request_id=uc.cdp.fetch.RequestId(request.request_id)))
            except:
                pass


# Create global instance
response_stage_processor = ResponseStageProcessor(None)  # Will be set by dynamic_hook_system