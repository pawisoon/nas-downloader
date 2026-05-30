from app import create_app

application = create_app()

if __name__ == "__main__":
    # Flask dev server. Single-user NAS tool, single process. threaded=True
    # is required for SSE (each event stream needs its own thread).
    application.run(host="0.0.0.0", port=8080, threaded=True, debug=False)
