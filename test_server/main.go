package main

import (
	"net/http"
	"fmt"
	"time"
)

func main() {

	var srv http.Server
	srv.Addr = ":8080"

	http.HandleFunc("/", index_main)
	http.HandleFunc("/redirect", index_redirect)

	fmt.Println("Listening for HTTP/2 connections on port 8080")

	srv.ListenAndServeTLS("test_server/certs/localhost.cert", "test_server/certs/localhost.key")
}

func index_redirect(w http.ResponseWriter, r *http.Request) {
	http.Redirect(w, r, "https://localhost:8080/", 301)
}

func index_main(w http.ResponseWriter, r *http.Request) {
	w.Header().Set("Access-Control-Allow-Origin", "*")

	time.Sleep(time.Second * 2)

	// allow pre-flight headers
	w.Header().Set("Access-Control-Allow-Headers", "Content-Range, Content-Disposition, Content-Type, ETag")

	r.Write(w)
}
