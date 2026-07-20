from __future__ import annotations

from fastapi import APIRouter

router = APIRouter(prefix="/api/v1/guide", tags=["guide"])


@router.get("")
async def get_guide():
    return {
        "title": "Welcome to World of Agents",
        "subtitle": "An intelligent agent platform that can help you with various tasks. "
        "Select a scenario below to get started, or just type your question directly.",
        "steps": [
            {
                "emoji": "\ud83e\udd16",
                "title": "Triage Agent",
                "description": "The default agent that understands your request and routes it to the right specialist.",
            },
            {
                "emoji": "\ud83d\udcbb",
                "title": "Scenario Management",
                "description": "Choose from different scenarios like code review or writing assistance.",
            },
            {
                "emoji": "\ud83d\udd04",
                "title": "Session Management",
                "description": "Your conversations are organized into sessions. Switch between them anytime.",
            },
            {
                "emoji": "\ud83c\udfa8",
                "title": "Customizable Theme",
                "description": "Switch between Dark, Light, Forest, and Sepia themes using the menu button.",
            },
        ],
    }
