// entry point for the whole app
import React from 'react';
import ReactDOM from 'react-dom/client';
import {createBrowserRouter, RouterProvider} from 'react-router-dom';
import App from './App.jsx';

// get pages
import LoginPage from './pages/Login.jsx';
import CommunicationsPage from './pages/CommunicationsPage.jsx';
import ChatbotPage from './pages/ChatbotPage.jsx';
import ResourcesPage from './pages/ResourcesPage.jsx';
import './index.css';

// make router to handle navigation w/ outlet
const router = createBrowserRouter([
    {
        path: '/login',
        element: <Login />,
    }
    ,
    {
        path: '/',
        element: <App />,
        children: [
            {
                index: true,
                element: <CommunicationsPage />,
            },
            {
                path: 'chatbot',
                element: <ChatbotPage />,
            },
            {
                path: 'resources',
                element: <ResourcesPage />,
            },
        ],
    },
]);

// create and render router
ReactDOM.createRoot(document.getElementById('root')).render(
    <React.StrictMode>
        <RouterProvider router={router} />
    </React.StrictMode>
);
