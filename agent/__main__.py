try:
    from .daemon import main
except ImportError:
    from agent.daemon import main
main()
