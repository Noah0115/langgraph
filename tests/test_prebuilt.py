from typing import Any, Callable, Dict, List, Optional, Sequence, Type, Union

from langchain_core.callbacks import (
    CallbackManagerForLLMRun,
)
from langchain_core.language_models import (
    BaseChatModel,
    LanguageModelInput,
)
from langchain_core.messages import (
    AIMessage,
    BaseMessage,
    HumanMessage,
    SystemMessage,
    ToolMessage,
)
from langchain_core.outputs import ChatGeneration, ChatResult
from langchain_core.pydantic_v1 import BaseModel
from langchain_core.runnables import Runnable, RunnableLambda
from langchain_core.tools import BaseTool

from langgraph.prebuilt import ToolNode, create_react_agent


class FakeToolCallingModel(BaseChatModel):
    def _generate(
        self,
        messages: List[BaseMessage],
        stop: Optional[List[str]] = None,
        run_manager: Optional[CallbackManagerForLLMRun] = None,
        **kwargs: Any,
    ) -> ChatResult:
        """Top Level call"""
        messages_string = "-".join([m.content for m in messages])
        message = AIMessage(content=messages_string, id="0")
        return ChatResult(generations=[ChatGeneration(message=message)])

    @property
    def _llm_type(self) -> str:
        return "fake-tool-call-model"

    def bind_tools(
        self,
        tools: Sequence[Union[Dict[str, Any], Type[BaseModel], Callable, BaseTool]],
        **kwargs: Any,
    ) -> Runnable[LanguageModelInput, BaseMessage]:
        if len(tools) > 0:
            raise ValueError("Not supported yet!")
        return self


def test_no_modifier():
    model = FakeToolCallingModel()
    agent = create_react_agent(model, [])
    inputs = [HumanMessage("hi?")]
    response = agent.invoke({"messages": inputs})
    expected_response = {"messages": inputs + [AIMessage(content="hi?", id="0")]}
    assert response == expected_response


def test_system_message_modifier():
    model = FakeToolCallingModel()
    messages_modifier = SystemMessage(content="Foo")
    agent = create_react_agent(model, [], messages_modifier=messages_modifier)
    inputs = [HumanMessage("hi?")]
    response = agent.invoke({"messages": inputs})
    expected_response = {"messages": inputs + [AIMessage(content="Foo-hi?", id="0")]}
    assert response == expected_response


def test_system_message_string_modifier():
    model = FakeToolCallingModel()
    messages_modifier = "Foo"
    agent = create_react_agent(model, [], messages_modifier=messages_modifier)
    inputs = [HumanMessage("hi?")]
    response = agent.invoke({"messages": inputs})
    expected_response = {"messages": inputs + [AIMessage(content="Foo-hi?", id="0")]}
    assert response == expected_response


def test_callable_modifier():
    model = FakeToolCallingModel()

    def messages_modifier(messages):
        return [HumanMessage(content="Bar")]

    agent = create_react_agent(model, [], messages_modifier=messages_modifier)
    inputs = [HumanMessage("hi?")]
    response = agent.invoke({"messages": inputs})
    expected_response = {"messages": inputs + [AIMessage(content="Bar", id="0")]}
    assert response == expected_response


def test_runnable_modifier():
    model = FakeToolCallingModel()

    messages_modifier = RunnableLambda(lambda x: [HumanMessage(content="Baz")])

    agent = create_react_agent(model, [], messages_modifier=messages_modifier)
    inputs = [HumanMessage("hi?")]
    response = agent.invoke({"messages": inputs})
    expected_response = {"messages": inputs + [AIMessage(content="Baz", id="0")]}
    assert response == expected_response


async def test_tool_node():
    def tool1(some_val: int, some_other_val: str) -> str:
        """Tool 1 docstring."""
        return f"{some_val} - {some_other_val}"

    async def tool2(some_val: int, some_other_val: str) -> str:
        """Tool 2 docstring."""
        return f"tool2: {some_val} - {some_other_val}"

    result = ToolNode([tool1]).invoke(
        {
            "messages": [
                AIMessage(
                    "hi?",
                    tool_calls=[
                        {
                            "name": "tool1",
                            "args": {"some_val": 1, "some_other_val": "foo"},
                            "id": "some 0",
                        }
                    ],
                )
            ]
        }
    )
    tool_message: ToolMessage = result["messages"][-1]
    assert tool_message.type == "tool"
    assert tool_message.content == "1 - foo"
    assert tool_message.tool_call_id == "some 0"

    result2 = await ToolNode([tool2]).ainvoke(
        {
            "messages": [
                AIMessage(
                    "hi?",
                    tool_calls=[
                        {
                            "name": "tool2",
                            "args": {"some_val": 2, "some_other_val": "bar"},
                            "id": "some 1",
                        }
                    ],
                )
            ]
        }
    )
    tool_message: ToolMessage = result2["messages"][-1]
    assert tool_message.type == "tool"
    assert tool_message.content == "tool2: 2 - bar"
