import streamlit as st
from dotenv import load_dotenv
from langchain_openai import ChatOpenAI
from langchain.agents import Tool, AgentExecutor, create_react_agent
from langchain_core.prompts import ChatPromptTemplate, PromptTemplate
from langchain_community.utilities import SQLDatabase
from langchain_core.runnables import RunnablePassthrough
from langchain_core.output_parsers import StrOutputParser
from langchain_core.messages import AIMessage, HumanMessage
from langchain_community.utilities import SerpAPIWrapper
from langchain import hub

def init_database(user: str, password: str, host: str, port: str, database:str) -> SQLDatabase:
    db_uri = f"postgresql://{user}:{password}@{host}:{port}/{database}"
    return SQLDatabase.from_uri(db_uri)

def get_sql_chain(db):
    template = """
        You are an expert pharmacologist analyzing a drug interaction database at a company. 
        You are interacting with a user who is asking you questions about the company's database.
        Based on the table schema below, write a SQL query that would answer the user's question. Take the conversation history into account.
              
        <SCHEMA>(schema)</SCHEMA>
        
        Conversation History: {chat_history}
        
        Write only the SQL query and nothing else. Do not wrap the SQL query in any other text, not even backticks.
        
        For example:
        Question: What's the interaction of Lepirudin and Apixaban?
        SQL Query: SELECT description FROM ddi WHERE (drug_name = 'Lepirudin' AND interacting_drug_name = 'Apixaban');
        
        Question: Show me interactions for anticoagulant drugs only?
        SQL Query: SELECT * FROM ddi WHERE drug_name = 'Lepirudin' AND description LIKE '%anticoagulant%' LIMIT 5;
        
        Your turn:
        Question: {question}
        SQL Query:
        """
    
    prompt = ChatPromptTemplate.from_template(template)
    
    llm = ChatOpenAI(model="gpt-3.5-turbo", temperature=0.3)
    
    def get_schema(_):
        return db.get_table_info()
    
    return(
        RunnablePassthrough.assign(schema=get_schema)
        | prompt
        | llm
        | StrOutputParser()
    )

def get_db_response(user_query: str, db: SQLDatabase, chat_history: list):
    sql_chain = get_sql_chain(db)
    
    template = """
        You are an expert pharmacologist analyzing a drug interaction database at a company. 
        You are interacting with a user who is asking you question about the company's database.
        Based on the table schema below, sql query, and sql response, write a natural language response.
                
        <SCHEMA>(schema)</SCHEMA>
        
        Conversation History: {chat_history}
        SQL Query: <SQL>{query}</SQL>
        User question: {question}
        SQL Response: {response}
        """
    
    prompt = ChatPromptTemplate.from_template(template)
        
    llm = ChatOpenAI(model="gpt-3.5-turbo")
    
    chain = (
        RunnablePassthrough.assign(query=sql_chain).assign(
            schema = lambda _: db.get_table_info(),
            response = lambda vars: db.run(vars["query"]),
        )
        | prompt
        | llm
        | StrOutputParser()   
    )
    
    return chain.stream({
        "question": user_query,
        "chat_history": chat_history,
    })

def get_serp_agent():
    """Create a SERP agent with proper error handling"""
    try:
        search = SerpAPIWrapper()
        
        search_tool = Tool(
            name="search",
            func=search.run,
            description="Use this tool to search the web for drug information, pharmacology, side effects, and other general pharmaceutical queries."
        )

        llm = ChatOpenAI(model="gpt-3.5-turbo", temperature=0.3)
        tools = [search_tool]
        
        # Use the newer create_react_agent approach
        try:
            # Try to get the react prompt from hub
            prompt = hub.pull("hwchase17/react")
        except:
            # Fallback to manual prompt if hub is not available
            prompt = PromptTemplate.from_template("""
            Answer the following questions as best you can. You have access to the following tools:

            {tools}

            Use the following format:

            Question: the input question you must answer
            Thought: you should always think about what to do
            Action: the action to take, should be one of [{tool_names}]
            Action Input: the input to the action
            Observation: the result of the action
            ... (this Thought/Action/Action Input/Observation can repeat N times)
            Thought: I now know the final answer
            Final Answer: the final answer to the original input question

            Begin!

            Question: {input}
            Thought: {agent_scratchpad}
            """)
        
        agent = create_react_agent(llm, tools, prompt)
        
        return AgentExecutor(
            agent=agent, 
            tools=tools, 
            verbose=False,
            handle_parsing_errors=True,
            max_iterations=3
        )
    except Exception as e:
        st.error(f"Error initializing SERP agent: {str(e)}")
        return None

def is_db_query(user_query: str, chat_history: list) -> bool:
    """Determine if the query should be routed to the database or web search."""
    llm = ChatOpenAI(model="gpt-3.5-turbo", temperature=0)
    
    template = """
    You are an expert at determining what type of pharmaceutical query a user is asking about.
    
    Your task is to STRICTLY categorize the following query:
    
    ONLY respond with "DATABASE" if the query is SPECIFICALLY about:
    1. Drug-to-drug interactions between two specific medications
    2. Direct questions about the database itself (schema, statistics, number of entries)
    
    For ALL other queries, including but not limited to:
    - Drug compositions
    - Synthesis methods
    - Side effects (unless specifically about interaction side effects)
    - Pharmacology
    - Chemical properties
    - Manufacturing
    - History or discovery
    - Mechanisms of action
    - Dosage information
    
    You MUST respond with "SEARCH" for these types of queries.
    
    Conversation History: {chat_history}
    User Query: {question}
    
    Response (ONLY "DATABASE" or "SEARCH"):
    """
    
    prompt = ChatPromptTemplate.from_template(template)
    
    chain = (
        prompt 
        | llm 
        | StrOutputParser()
    )
    
    result = chain.invoke({
        "question": user_query,
        "chat_history": chat_history
    })
    
    return "DATABASE" in result.upper()

def get_serp_response(user_query: str):
    """Get response from SerpAPI agent with proper error handling"""
    agent_executor = get_serp_agent()
    
    if agent_executor is None:
        def error_generator():
            yield "Sorry, I'm having trouble accessing web search at the moment. Please try again later."
        return error_generator()
    
    try:
        response = agent_executor.invoke({"input": user_query})
        
        # Extract the output from the response
        if isinstance(response, dict) and "output" in response:
            final_response = response["output"]
        else:
            final_response = str(response)
        
        def response_generator():
            yield final_response
        
        return response_generator()
    
    except Exception as e:
        def error_generator():
            yield f"I encountered an error while searching for information: {str(e)}. Please try rephrasing your question."
        return error_generator()

def get_response(user_query: str, db: SQLDatabase, chat_history: list):
    # Determine which tool to use
    if is_db_query(user_query, chat_history):
        # Use Tool 1: Database Query
        return get_db_response(user_query, db, chat_history)
    else:
        # Use Tool 2: SerpAPI Search
        return get_serp_response(user_query)

if "chat_history" not in st.session_state:
    st.session_state.chat_history = [
        AIMessage("Hi there! I'm a pharmacologist assistant. Ask me anything about drug interactions or general drug information.")
    ]

load_dotenv()

st.set_page_config(page_title= "Pharmacy Assistant", page_icon=":pill:")

st.title("Pharmacy Assistant 💊")

with st.sidebar:
    st.subheader("Settings")
    st.write("This is a chatbot powered by custom LLM Agents. Connect to the database and start chatting.")
    
    st.text_input("Host", value="localhost", key="Host")
    st.text_input("Port", value="5432", key="Port")
    st.text_input("User", value="postgres", key="User")
    st.text_input("Password", type="password", value="111", key="Password")
    st.text_input("Database", value="postgres", key="Database")
    
    if st.button("Connect"):
        with st.spinner("Connecting to database..."):
            try:
                db = init_database(
                    st.session_state["User"],
                    st.session_state["Password"],
                    st.session_state["Host"],
                    st.session_state["Port"],
                    st.session_state["Database"]
                )
                st.session_state.db = db
                st.success("Connected to database!")
            except Exception as e:
                st.error(f"Failed to connect to database: {str(e)}")

for message in st.session_state.chat_history:
    if isinstance(message, AIMessage):
        with st.chat_message("AI"):
            st.markdown(message.content)
    if isinstance(message, HumanMessage):
        with st.chat_message("Human"):
            st.markdown(message.content)

user_query = st.chat_input("Ask me about drug interactions or general drug information...")
if user_query is not None and user_query.strip() !="":
    st.session_state.chat_history.append(HumanMessage(content=user_query))
    
    with st.chat_message("Human"):
        st.markdown(user_query)
    
    with st.chat_message("AI"):
        try:
            # Check if database is connected for database queries
            if is_db_query(user_query, st.session_state.chat_history) and "db" not in st.session_state:
                error_msg = "Please connect to the database first to query drug interactions."
                st.error(error_msg)
                st.session_state.chat_history.append(AIMessage(content=error_msg))
            else:
                db = st.session_state.get("db", None)
                response = st.write_stream(get_response(user_query, db, st.session_state.chat_history))
                st.session_state.chat_history.append(AIMessage(content=response))
        except Exception as e:
            error_msg = f"Error: {str(e)}. Please try again or check your connection."
            st.error(error_msg)
            st.session_state.chat_history.append(AIMessage(content=error_msg))