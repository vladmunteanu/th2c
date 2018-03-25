package main

import (
	"fmt"
	"golang.org/x/net/http2"
	"net/http"
)

var mux map[string]func(http.ResponseWriter, *http.Request)

func main() {
	server := &http.Server{
		Addr:    ":8080",
		Handler: &httpHandler{},
	}

	http2.ConfigureServer(server, &http2.Server{MaxConcurrentStreams: 1})

	mux = make(map[string]func(http.ResponseWriter, *http.Request))
	mux["/"] = indexMain
	mux["/redirect"] = indexRedirect

	fmt.Println("Listening for HTTP/2 connections on port 8080")
	server.ListenAndServeTLS("test_server/certs/localhost.cert", "test_server/certs/localhost.key")
}

type httpHandler struct{}

func (*httpHandler) ServeHTTP(w http.ResponseWriter, r *http.Request) {
	if h, ok := mux[r.URL.String()]; ok {
		h(w, r)
		return
	}
}

func indexRedirect(w http.ResponseWriter, r *http.Request) {
	http.Redirect(w, r, "https://localhost:8080/", 301)
}

func indexMain(w http.ResponseWriter, r *http.Request) {
	w.Header().Set("Access-Control-Allow-Origin", "*")

	// allow pre-flight headers
	w.Header().Set("Access-Control-Allow-Headers", "Content-Range, Content-Disposition, Content-Type, ETag")

	r.Write(w)
}
