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
		Handler: &myHandler{},
	}

	http2.ConfigureServer(server, &http2.Server{MaxConcurrentStreams: 1})

	mux = make(map[string]func(http.ResponseWriter, *http.Request))
	mux["/"] = indexMain

	fmt.Println("Listening for HTTP/2 connections on port 8080")
	server.ListenAndServeTLS("test_server/certs/localhost.cert", "test_server/certs/localhost.key")
}

type myHandler struct{}

func (*myHandler) ServeHTTP(w http.ResponseWriter, r *http.Request) {
	if h, ok := mux[r.URL.String()]; ok {
		h(w, r)
		return
	}
}

func indexMain(w http.ResponseWriter, r *http.Request) {
	w.Header().Set("Access-Control-Allow-Origin", "*")

	// allow pre-flight headers
	w.Header().Set("Access-Control-Allow-Headers", "Content-Range, Content-Disposition, Content-Type, ETag")

	r.Write(w)
}
